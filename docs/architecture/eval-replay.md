# Eval 与 Replay 架构 (Eval-Replay)

> 评估是只读的事实判断，回放是带冻结谱系的新运行。两者都不猜测、不打分、不做模型裁判。

## 1. Metrics vs Eval

| 维度 | Metrics (`herdr/metrics.py`) | Eval (`herdr/eval_engine.py` + `herdr/eval_store.py`) |
| --- | --- | --- |
| 问题 | Run 聚合了多少事实（计数与状态） | Run 有哪些可验证的要求、验证、人工介入和终态事实 |
| 写状态 | 否（纯聚合） | `evaluate_run` 否；仅 `record_run_eval` 写一条事实 |
| 输入 | Trajectory 事件 + Task 归属 + 计数 | Task 权威状态 + 严格 verification 事实 + 自有 steering |
| 输出 | `HarnessRunMetrics`（计数、耗时、`final_status`、`task_completed`） | Eval 结果（四项事实、`warnings`、`task_id`、`workflow_id`、`evidence`） |
| 空值 | 缺失即零值或 `None`（聚合语义） | 缺失即 `null`（判断语义，见第 2 节） |

铁律：Eval 绝不导入 `observer` / `decision` / `metrics` / `intervention`；
Eval 表与 Metrics 聚合无外键、无同步投影，删除 Workflow 不删除审计行。

## 2. Null 语义

- Eval 不生成 verdict。`requirements_satisfied` 仅当 Task 已完成且存在严格 verification 布尔事实时返回该布尔值；其他情况为 `null`。
- `verification_passed: true | false | null`：仅接受严格 verification 事实（`verification.passed` 为真实布尔值）；缺失、非布尔、腐坏载荷一律 `null`，并记 `insufficient_verification`。
- `human_intervention_count` 仅统计归属 Task 的人类 steering；无归属 Task 时为 `null`，跨 Run 不借用。
- `final_status` 直接取归属 Task 的权威当前状态；没有归属 Task 时为 `null`。不从 stage verdict 或轨迹聚合状态推导。
- 禁止 `scores` 打分：Eval 行不暴露 verdict/scores/task_status 等重复投影。SQLite 旧列只为既有数据库兼容保留，新写入为空。

## 3. Lineage（谱系）

- `replay_specs` 表是 `source_run_id -> replay_run_id` 的谱系边（`replay_run_id` 唯一，同值幂等返回已存在行）。
- `get_replay_lineage(replay_run_id)` 沿边回走到源头，返回从最老祖先到自身的 Run 列表；未知返回 `[]`；成环时截断。
- 记录谱系边永不写源 Run 的 Task、Trajectory 或状态行；回放的新 Run 用全新 `replay_run_id` / `workflow_id` / `task_id`。

## 4. 冻结（Freeze）

- 每个回放定义在落盘前必须冻结为 Run 私有不可变文件（`projects.freeze_run_definition`），只写文件、不碰 Run/Task 行。
- 默认行为（`definition is None`）：只读取源 Run 自己的 ReplaySpec 快照，或源 Workflow 指向其 run-private `workflow.json` 的路径；缺失、不可读或损坏时拒绝 Replay。显式 `definition`（CLI 的 `--definition` / `--definition-file`）可提供定义覆盖。
- 每个目标 Replay 定义都必须成功冻结；冻结失败即拒绝创建 workflow、ReplaySpec 或 Task。`dry_run` 只规划不写，目标 `snapshot` 为 `None`；没有 source snapshot 时仍须提供显式定义。
- `ReplaySpec` 落盘冻结引用：`snapshot` / `snapshot_path` / `frozen_config_ref`（同一路径的三别名）指向冻结文件；`workflow_file` 优先取 `snapshot`。

## 5. Override（Policy）

- `replay_run(..., policy=None)` 接受映射或 JSON 对象字符串；落盘到 `replay_specs.policy_json`，读回为 `policy` / `policy_override`（同值双别名）。
- Policy 同时写入新 Task 载荷的 `agent_policy` 字段，作为回放节点的策略覆盖；`launch_argv` 按覆盖后的 `agent` / `goal` / `prompt` 构建。
- CLI：`herdr-task replay --policy '<JSON对象>'` 或 `--policy-file <路径>`；`--agent/--goal/--prompt/--source` 覆盖源 Run 对应字段。默认执行真实 preflight 与 `herdr-task launch`；launch 接收 Replay 指定的 Run ID 与 `replay_of` 并由现有运行管线写 Task 和执行事件。`--dry-run` 不执行启动或写入。

## 6. Compare（对比）

- `compare_evals(before, after)` 是纯函数事实对比，任一侧可为 `null`。
- Compare 仅输出 `before` / `after` 两组四字段：`requirements_satisfied`、`verification_passed`、`human_intervention_count`、`final_status`。缺失侧保持四个 `null`；不输出 winner、score、rank 或 verdict。
- CLI `herdr-task eval-compare --before-run/--before-revision --after-run/--after-revision` 只从持久化行读取再忠实输出四字段。

## 7. 隔离

- 身份隔离：`run_id_for_task(task) == run_id` 校验归属；错配记 `task_ownership_mismatch` 且不泄漏外来 `task_id` / `workflow_id` / 状态；回放 Eval 不统计源 Run 的 steering。
- 并发：`record_eval_result` 用 `BEGIN IMMEDIATE` + `max+1` + 冲突重试分配 `revision`，同 `(run_id, revision)` 幂等返回已存在行；`record_replay_spec` 同 `replay_run_id` 幂等。
- 旁路隔离：诊断与证据采集失败不改变执行状态；外部调用有超时（launch/preflight 探针默认 120s），后台任务有并发上限。

## 8. 局限（当前不做）

- 不做 LLM 裁判（judge）：结论只来自程序可验证的权威状态与严格事实。
- 不做评分 / 排名：没有分数、权重、排序与阈值调参。
- 不做自动优化：Eval/Replay 不改写源 Run，不自动重试、重做或提升结论。
- 不做 Workspace Memory：回放只带冻结定义与 lineage 边，不搬运工作区现场、记忆或跨 Run 上下文。

## 9. 调用链速查

- 只读评估：`herdr-task eval --run-id <run> [--json]` → `eval_engine.evaluate_run`。
- 落盘评估：`herdr-task eval --run-id <run> --record [--revision N]` → `record_run_eval` → `eval_store.record_eval_result`。
- 事实对比：`herdr-task eval-compare --before-run A --after-run B` → `compare_evals`。
- 回放：`herdr-task replay --source-run <run> [--definition/--definition-file] [--policy/--policy-file] [--dry-run]` → `replay_engine.replay_run` → preflight → `freeze_run_definition` + `register_workflow` + `record_replay_spec` → `herdr-task launch` 创建 Run/Task 并启动执行。
