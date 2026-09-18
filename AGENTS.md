# HAFlow Multi-Agent System Map

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> 本文件仅作为 AI 注入上下文的导航目录（约 100 行），严禁展开具体实现细节。所有深度上下文必须跟随下方链接查阅。

---

## 0. 知识层与持久理解 (LLM Wiki)

- **知识层主索引**: [`wiki/index.md`](file:///Users/user/HAFlow/wiki/index.md) — 真实代码沉淀的知识图谱与意图寻路主入口。
- **Wiki 治理规范**: [`wiki/WIKI.md`](file:///Users/user/HAFlow/wiki/WIKI.md) 与 [`wiki/log.md`](file:///Users/user/HAFlow/wiki/log.md) — 当变更涉及架构、模型、状态机、DAG 算法、自愈逻辑或路由时，必须同步更新对应 Wiki。
- **工程教训知识库**: [`docs/lessons/lessons-learned.md`](file:///Users/user/HAFlow/docs/lessons/lessons-learned.md) — 跨模块可复用工程教训（四段式）。使用 `.agents/skills/knowledge-capture/` 技能在收尾时沉淀。

---

## 1. 核心架构与事实来源 (Source of Truth)

- **系统定位与全局架构**: [`architecture-overview.md`](file:///Users/user/HAFlow/docs/architecture/architecture-overview.md) — 分层设计、组件职责、状态机。
- **空间现场模型**: [`tab-node-model.md`](file:///Users/user/HAFlow/docs/architecture/tab-node-model.md) — Tab=Node, Pane=Workspace, Anchor 锚点现场隔离。
- **工作流使用手册**: [`universal-workflow-guide.md`](file:///Users/user/HAFlow/docs/guides/universal-workflow-guide.md) — 端到端工作流调度、派发与自愈实操。
- **模板开发规范**: [`template-authoring-guide.md`](file:///Users/user/HAFlow/docs/guides/template-authoring-guide.md) 与 [`workflow-template-schema.md`](file:///Users/user/HAFlow/docs/product-specs/workflow-template-schema.md) — YAML 语法契约与 DAG 校验规则。
- **路由与调度策略**: [`agent-policy-spec.md`](file:///Users/user/HAFlow/docs/product-specs/agent-policy-spec.md) — Node 级 Agent 策略、健康准入、锁预占。
- **服务运维与守护排障**: [`service-management.md`](file:///Users/user/HAFlow/docs/operations/service-management.md) 与 [`troubleshooting-faq.md`](file:///Users/user/HAFlow/docs/operations/troubleshooting-faq.md) — LaunchAgent 启停与死锁救援。
- **沙盒深度探针**: [`deep-preflight-playbook.md`](file:///Users/user/HAFlow/docs/operations/deep-preflight-playbook.md) — 各 Agent 无副作用探针机制。
- **全量 CLI 参考**: [`cli-reference.md`](file:///Users/user/HAFlow/docs/references/cli-reference.md) — 命令行参数字典。
- **Agent 交付演进**: [`walkthroughs/`](file:///Users/user/HAFlow/docs/walkthroughs/README.md) — 各类 Agent 任务交付演进报告与 Walkthrough 归档。

---

## 2. 代码分层与物理地图 (Codebase Map)

- **`bin/` (CLI 入口)**:
  - [`herdr-factory`](file:///Users/user/HAFlow/bin/herdr-factory): 工作流与项目生命周期主入口。
  - [`herdr-task`](file:///Users/user/HAFlow/bin/herdr-task): 任务派发、基线验收与运行时自愈工具。
  - [`herdr-preflight`](file:///Users/user/HAFlow/bin/herdr-preflight) / [`herdr-deep-preflight`](file:///Users/user/HAFlow/bin/herdr-deep-preflight): Agent 健康体检与沙盒探活。
- **`services/` (后台常驻守护进程)**:
  - [`herdr-controller.py`](file:///Users/user/HAFlow/services/herdr-controller.py): DAG 依赖推进与协调器分发核心。
  - [`herdr-sentinel.py`](file:///Users/user/HAFlow/services/herdr-sentinel.py): Tab/Pane 存活巡检看门狗。
  - [`herdr-notifier.py`](file:///Users/user/HAFlow/services/herdr-notifier.py): macOS 原生通知广播。
  - [`herdr-worker.py`](file:///Users/user/HAFlow/services/herdr-worker.py): 独立 CoW Git 克隆与工位装配。
- **`herdr/` (核心业务库包)**:
  - [`workflow.py`](file:///Users/user/HAFlow/herdr/workflow.py): Kahn 算法 DAG 拓扑校验、模板解析与就绪节点计算。
  - [`agent_router.py`](file:///Users/user/HAFlow/herdr/agent_router.py): Node 级策略匹配、健康准入与 Reservation 并发锁。
  - [`projects.py`](file:///Users/user/HAFlow/herdr/projects.py): 多项目注册表与 `ensure_node_runtime` 探活自愈。
  - [`topology.py`](file:///Users/user/HAFlow/herdr/topology.py): 拓扑现场动态自愈与 Anchor 重建。
  - [`workflow_docs.py`](file:///Users/user/HAFlow/herdr/workflow_docs.py): Workflow 共享文档区（clone 外追加式账本、provenance 与 stale 治理）。
  - [`pane_pool.py`](file:///Users/user/HAFlow/herdr/pane_pool.py): 智能体工位 (Pane) 槽位管理。
  - [`preflight.py`](file:///Users/user/HAFlow/herdr/preflight.py) / [`deep_preflight.py`](file:///Users/user/HAFlow/herdr/deep_preflight.py): 探针实现。
- **`workflow_templates/`**: 内置 YAML 工作流模板。
- **`console/`**: HAFlow 控制台（产品名 `PRODUCT_NAME`，见 `console/herdr_factory_console.py`）的仓库内 canonical source；通过 `scripts/install-herdr-console.sh` 部署到 `~/.herdr-console`。
- **`scripts/install-herdr-console.sh`**: 同步 Console 前端并按需重启 LaunchAgent。
- **`tests/`**: [`test_workflow_engine.py`](file:///Users/user/HAFlow/tests/test_workflow_engine.py) 核心算法与引擎测试。

---

## 3. 运行与开发约束指针

- **作业规范与红线约束**: 必须严格遵循 [`RULES.md`](file:///Users/user/HAFlow/RULES.md) 执行开发。
- **操作入口与环境避坑**: 常用命令与运维陷阱请直接参考 [`CLAUDE.md`](file:///Users/user/HAFlow/CLAUDE.md)。

---

## 4. 任务收尾 SOP（合并 PR 前的强制前置步骤）

### 收尾第 1 步：知识沉淀（在合并 PR 之前）

- 检查本次 session 是否排查了复杂 bug、解决了同类复发问题、或踩了技术坑；
- 凡符合通用教训的，按四段式规范追加到 [`docs/lessons/lessons-learned.md`](file:///Users/user/HAFlow/docs/lessons/lessons-learned.md)；
- 对 Agent 说 **"沉淀一下本次 session 的知识"** 或 **"归档教训"**，Agent 自动执行 `.agents/skills/knowledge-capture/` 流程；
- 将知识更新与代码**一同提交到同一个 PR**（禁止事后单独补提 PR）。

### 收尾第 2 步：验收清单

按 `RULES.md §3` 逐项核对后，方可合并 PR。
