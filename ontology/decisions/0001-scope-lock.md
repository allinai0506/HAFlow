# 0001 Scope Lock — HAFlow 第一业务切片

日期：2026-10-19
模式：BROWNFIELD_DISCOVERY（首轮 READ_ONLY，仅新增 ontology/ 资产）
基线：origin/main @ 6b4d48b（CoW 沙盒 `~/.sandboxes/ontology-brownfield-discovery-v1`，分支 `feat/ontology-brownfield-discovery-v1`）

## Goal

为 HAFlow 的“项目 -> 工作流 -> 节点 -> 任务”编排闭环建立可版本化、可验证、可评审的候选本体，
把当前分散在 Python 代码、SQLite StateDB、JSON 注册表、YAML 模板与 Wiki 中的隐式业务语义显式化。

## Non-goals

- 不覆盖 Console UI、Trajectory/Observer/Supervisor、Experience Hub（除必要引用）——留作后续切片
- 不修改任何业务代码、数据库脚本或生产配置
- 不做数据库表/字段到对象的 1:1 自动生成
- 不自动把任何候选项升级为 `active`（必须经过 Evidence -> Review -> Test -> Merge）

## First business slice

`Project -> WorkflowDefinition/WorkflowInstance -> WorkflowNode -> Task -> AgentReservation`
外加 Task / Workflow 生命周期、Gate 门禁、Artifact 交付物、Event 事件账本、Checkpoint 快照。

## Systems in scope

- Python 微内核：`herdr/`（projects、workflow、transitions、kernel、state_db、agent_router、node_capacity、deliverables…）
- CLI：`bin/herdr-task`、`bin/herdr-factory`
- 常驻服务：`services/herdr-controller.py`
- 持久化：`~/.herdr-controller/state.db`（权威）与 JSON 投影/注册表
- 模板：`workflow_templates/*.yaml`

## Read/write boundaries

- 首轮只读业务代码与运行态；只写 `ontology/**` 新文件
- 不启动真实 Agent、不调用收费模型、不触碰生产 `~/.herdr-controller` 状态

## Acceptance tests

1. `ontology_validate.py ontology` 通过（schema/引用完整性）
2. `ontology_index.py ontology` 生成 `generated/index.json`
3. 每个正式项带 Evidence Ref（kind/source/locator/confidence）
4. Golden case 覆盖主路径与关键异常路径
5. 语义冲突进入 `decisions/0003-semantic-conflicts.md`，不自行裁决
