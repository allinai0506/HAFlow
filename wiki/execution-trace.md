# 任务执行轨迹

任务详情抽屉的「执行轨迹」是只读投影。它不调度 Agent，不推进工作流，也不写入第二套历史。

## 数据从哪来

1. 任务文档里的创建时间、状态历史、开始时间和提交 SHA。
2. 状态库里的任务行和 `events` 表。任务身份与该 `task_id` 且 `run_id` 匹配的事件都通过 `get_readonly_db_connection` 读取，不打开可写连接，也不做表结构迁移。
3. 能证明属于该任务的 Codex、Claude Code 或 OpenCode 原生会话。

投影代码在 `herdr/execution_trace.py`。原生解析在 `herdr/native_session_trace.py`。控制台入口是 `GET /api/task/trajectory`。

## 归属

`agent_session_id` 是 HAFlow 启动适配器记下的会话标识，不是 Codex、Claude Code 或 OpenCode 的原生会话 ID。不能拿它去对原生记录。

Trajectory 行没有 `run_id` 时保留为未知运行，不标成任务当前的 `run_id`。任务本身也没有 `run_id` 时，账本里 `run_<task_id>` 的行仍属于该任务；这是 `run_id_for_task()` 给旧任务写上的身份。状态历史优先用记录里的 `timestamp`。

Pane ID 会回收，也不能当作历史归属。

没有记录 `native_session_id` 时，只接受工作目录与该任务独占 `clone_path` 完全一致的原生会话。隔离目录被多个任务共用时，拒绝合并原生记录。已经写在任务、运行时或 `.herdr-launch-identity.json` 里的 `native_session_id` 必须与原生会话 ID 一致。身份文件的 `task_id` 对得上、但 `run_id` 属于另一次运行时，不用这份身份，也不退回按工作目录合并。

Claude 先按已记录的会话文件名直接打开。找不到时才逐个查看，并在时间预算或 8 个文件处停下，不先排序整个目录。Codex 也先按已记录的会话 ID 定位，再套用最多 800 个文件的扫描上限。`events` 先按当前运行（没有 `run_id` 的旧行仍保留；旧任务再加 `run_<task_id>`）和可投影的事件类型过滤，再取最多 2000 条。

超过 2000 条窗口时，窗口只保留最早的 2000 条，之后的权威回执会落在窗口外。此时按同一 Task / Run 范围补读 `verification_completed`、`task_artifact_checkpoint`、`candidate_published` 和 `tool_execution`，不改成无界查询。补读的行按事件 ID 去重，再按时间排序输出。

补读按事件类型分别限流：`task_artifact_checkpoint`、`candidate_published`、`tool_execution` 三类各自最多 2000 条，额度互不共用。共用一个额度会让某一类写满后仍然把更晚的其他类型挤到补读范围外，重新制造"看不到最新回执"的问题。这三类先取该类最新的 2000 条，再按时间正序还原，因此同类中最新的证据一定可见。`verification_completed` 走独立的单类补读查询，额度同样独立于其他三类。

打开一条轨迹时只解码该任务，以及 `clone_path` 规范化后与它相同的其他任务。其余任务的完整载荷留在库里。

同一次状态变化会同时写进状态历史和 `task_transition`。相同任务、目标状态和时间合成一条轨迹。

对不上的原生记录直接丢弃，页面仍显示已经归属的任务事件。

## 不推断的事

执行过测试命令不是测试通过。通过数等于总数也不是测试通过；同一份证据以 `verification_completed` 里的 `passed` 为准。任务状态变成 completed 不是全部工具成功。两条相似命令不是重试。最终工作区里的文件不是某一时刻的读取或编辑。没有原始时间就保持为空，不用当前时间填。

## 页面

打开工作流，点开任务，再点「执行轨迹」。这一页才请求轨迹。时间正序。失败事件用红色，通过用绿色，编辑用蓝色，读取用空心灰点。点一条事件展开脱敏后的工具、相对路径、命令名、退出码、来源和证据标识。

## 已保存的执行报告与产物回执

`FACT` 轨迹读取同一 Task / Run 的 `task_artifact_checkpoint`、`candidate_published` / `candidate_frozen` 和 `tool_execution`。阶段报告展示执行摘要、下一步、Observation ID 和 SHA-256；摘要复用现有脱敏能力。报告里的“测试通过”是执行者陈述，不转换为验收通过。派发事件展示任务目标与执行者，不返回完整提示词或完成凭据。

`FACT` 候选冻结写成 workflow 级事件，没有 `task_id`。轨迹按 `publication_event_id` 关联到引发冻结的 `candidate_published` 事件，再回到该发布事件所属的 Task / Run；只有发布任务看得到这次冻结。同一个 workflow 的其他任务、其他运行的发布事件，以及身份不足（缺少或指向未知发布事件）的冻结，都保持不归因，不猜测归属。

关联的发布事件必须带有与当前 Task 相同的 `run_id`。缺少 `run_id` 或为空的发布事件无法证明属于哪一次运行，接受它会让这次冻结出现在该任务的每一次后续运行里，因此同样保持不归因。

`FACT` 无原生工具适配（包括 Qoder）时明确提示缺口，已有报告仍可展示；`recorded_actions` 与原生 `agent_actions` 分开。存在具体操作或报告时，无详情的生命周期记录折叠保留。轨迹页的“本任务产出”复用产物页及其可读性、归属与失败语义。

Evidence: `herdr/execution_trace.py::project_stored_event/trace_availability`；`herdr/native_session_trace.py::load_native_actions`；`console/herdr_factory_console.py::renderExecutionTrace/renderTaskDrawer`；`tests/test_execution_trace_api.py::test_ledger_reports_dispatch_artifacts_and_candidate_without_native_session`。

`FACT` 轨迹页优先展示具体事件与执行者报告，再展示本任务产出；不重复渲染概览/运行时已有的完整属性卡，避免报告被推到首屏下方。Evidence: `renderTaskDrawer`；`tests/test_console_execution_trace.py::test_trace_drawer_prioritizes_execution_over_repeated_attributes_and_artifacts`。

## ego-browser 执行义务 V1

FACT：新 Workflow 可在节点声明 `required_executions`，由数据库首次注册冻结并绑定到 Task/Run/candidate。自动裁决入口只观察，证据写 ObservationStore，Ledger 引用核验结果；无契约历史任务保持原行为。

UNKNOWN：当前原生工具记录不能证明浏览器动作、服务加载候选与业务回读，因此生产适配器不能生成 satisfied。原生 shell 成功、Agent 报告或技能读取都不能补足此缺口。真实工具探针不等于冻结候选 E2E。

Evidence：`herdr/execution_obligations.py::verify_executions`、`herdr/ego_execution_evidence.py::observe_task`、`tests/test_execution_obligations.py`。完整信任边界、只观察覆盖范围及未来事务接入要求见 [执行义务 V1](../docs/engineering/ego-execution-contract-v1.md)。
