# CLI 全量命令参考手册 (CLI Reference)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> 本文档列出 HAFlow 编排系统中所有控制台与命令行工具的完整语法、选项及返回值说明。

---

## 1. `herdr-factory` 命令

项目与工作流生命周期控制工具。

### 1.1 `herdr-factory templates`
列出所有可用的工作流模板（包含内置与用户目录）。
```bash
herdr-factory templates
```

### 1.2 `herdr-factory run`
启动一个新的工作流实例并通知协调员。
```bash
herdr-factory run <requirement...> [--template <name>] [--project <path>] [--workflow-id <id>] [--agent <name>]
```
- `<requirement...>`：自然语言业务需求描述。
- `--template <name>`：指定工作流模板，默认 `software-development-v1`。
- `--project <path>`：指定本地 Git 仓库路径，默认当前所在目录。
- `--agent <name>`：强制指定全局 Agent，默认 `auto`（走路由器）。

### 1.3 `herdr-factory status`
展示指定 Workflow 各节点的执行状态。
```bash
herdr-factory status <workflow_id>
```

### 1.4 `herdr-factory doctor`
运行全局环境自检（Herdr 服务、Agent 探针、Git 状态等）。
```bash
herdr-factory doctor
```

---

## 2. `herdr-task` 命令

工单（Task）创建、生命周期与节点工位自愈工具。

### 2.1 `herdr-task launch`
创建并立即在目标节点派生工位执行 Task。
```bash
herdr-task launch \
  --workflow-id <id> \
  --node <node_id> \
  --goal "<任务目标>" \
  --criteria "<验收标准>" \
  [--task-type explore|plan|code|test|review|docs] \
  [--agent auto|<agent_name>] \
  [--source <project_root>]
```
> 注：`--stage` 可作为 `--node` 的兼容别名。
>
> Fix-loop 续接参数：`--onto <branch>` 让 Task 的 CoW Clone 直接检出现有分支
> （如开放中的 PR 分支），commit 落在该分支上直接更新 PR；origin 拉取后分支
> 不存在即 fail-fast，本地分支与 origin 分叉同样拒绝（仅允许本地领先）。
> `--supersedes <task_id>` 派发同时原子作废旧 Task（`failed→superseded` 合法）。

### 2.2 `herdr-task node-status`
查询 Workflow 指定节点的任务状态与 DAG 依赖摘要。
```bash
herdr-task node-status <workflow_id> <node_id>
```

### 2.3 `herdr-task ops-center`
输出运维驾驶舱聚合视图：老板视角、Workflow 卡片、Agent Fleet、异常清单与任务时长摘要。
```bash
herdr-task ops-center [--workflow-id <workflow_id>] [--include-tasks]
```

### 2.4 `herdr-task ensure-runtime`
对目标节点执行 Tab / Anchor Pane 的探活与自动自愈。
```bash
herdr-task ensure-runtime --workflow-id <workflow_id> --node <node_id>
```

### 2.5 `herdr-task status` / `list` / `cleanup`
```bash
# 查看任务状态
herdr-task status <task_id>

# 列出当前工作流的所有任务
herdr-task list --workflow-id <workflow_id>

# 逻辑清理(仅状态归档,pane/clone 保留)
herdr-task cleanup <task_id>
```

### 2.6 `herdr-task finalize`
单任务物理收尾:转写落盘 → 关 pane → 删 clone → 状态推进到 cleaned。幂等。
```bash
herdr-task finalize <task_id> [--force] [--purge-clones]
```
- `<task_id>`：目标任务。仅允许非活跃状态(`completed`/`committed`/`integrated`/`cleanup_ready`/`cleaned`/`superseded`)。
- `--force`：允许收尾 `failed` 任务(失败现场默认保留供排障)。
- `--purge-clones`：无 integration 证据(mode=none 的 docs 任务等)时也强制删除 clone。
- 证据:终端转写写入 `~/.herdr-controller/logs/tasks/<task_id>/terminal.log` 与 `meta.json`。
- clone 删除安全规则:有 `integration_ref` 或 `superseded` 才删;`committed` 未 integrate 拒删。

### 2.7 `herdr-task close-workflow`
工作流一键收尾(自动+手动两用):闸门校验 → 逐任务 finalize → 关阶段 tab → 标记完成 → 输出收尾报告。幂等。
```bash
herdr-task close-workflow <workflow_id> [--include-coordinator] [--purge-clones] [--dry-run] [--accept-escalated]
```
- 闸门:存在活跃任务(`pending`/`dispatched`/`working`/`blocked`/`agent_done`/`rework`)时中止。
- `failed` 任务默认保留现场,报告中列 `retained-failed`。
- `--accept-escalated` 显式接受已升级 Git Task 的当前结果；报告 outcome 为
  `escalated_accepted`，逐项报告 pane 是否关闭、Task 最终状态及未集成 Clone 保留原因。
  该确认不会把 `committed` 伪报为 `integrated`，也不会删除未集成 Clone。
- 关阶段 tab 前校验 tab 内无其他 workflow 的外来 pane,否则跳过并写入报告 `tabs_skipped`。
- 总指挥 pane 默认保留;知识沉淀并合并 PR 后用 `--include-coordinator` 一并关闭。
- 收尾后六步法：close-workflow 完成后（总指挥 pane 保留期间），用户/总指挥应先运行
  six-step-finish 技能的破坏性收尾步骤（步骤 3 最终确认 + 步骤 4-6），再运行
  `--include-coordinator` 关闭总指挥 pane。调用形式：
  `bash .agents/skills/six-step-finish/scripts/finish-task.sh <任务分支> --base <base_branch> --forge none`
  （`<任务分支>` 为已合入的任务分支名，多个分支分别执行；绝对兜底
  `~/.agents/skills/six-step-finish/`）。
- 自动触发:Controller 在 `[WORKFLOW COMPLETE]` 时自动调用(等价于不带 flags)。
- 零任务的已登记 workflow 视为平凡完成,直接标记。

### 2.8 `herdr-task check-delivery`
校验交付候选并输出 PR 模板；读取当前 GitHub 分支已合并 PR 和本地 Git tree，
检查相同分支不同 SHA 的历史交付告警。该告警不阻断，但 transport 不可用时会输出输入缺失提示。
```bash
herdr-task check-delivery --workflow-id <id> --head-sha <sha> --head-ref <branch> [--repo-path <git-repo>]
```
- 缺少或歧义的有效 delivery、被 invalidation 的候选：exit 2。
- test/review launch 没有有效 delivery baseline 或 baseline 不包含候选：exit 2，
  写入 StateStore `test_baseline_rejected` actionable event；先运行 `record-delivery` 补齐身份。

---

### 2.9 `herdr-task reopen-workflow`
重开已关闭（completed）的 workflow，续用原有 workflow/PR 上下文做 fix-loop。
```bash
herdr-task reopen-workflow <workflow_id>
```
- 前置：registry 状态必须为 `completed`，且总指挥 pane 存活；
- 动作：状态翻转为 `in_progress`、重置阶段锁、置 `suppress_auto_close` 闩
  （防止周期 sweep 在首个 fix task 派发前将 workflow 自消除回 completed；
  闩在任一任务进入 ACTIVE 状态时自动摘除）；
- 已拆除的 clone/pane 不恢复，由后续 `launch` 的 `ensure_node_runtime` 按需重建。

### 2.9 `herdr-task set`（门禁 verdict 扩展）
```bash
herdr-task set <task_id> completed --verdict pass|blocked [--note "<blocker 清单>"]
```
- `--verdict` 仅在目标状态为 `completed` 时合法；`blocked` 必须带 `--note`；
- 落盘为任务的 `stage_verdict` / `stage_verdict_note` 字段，Controller 的
  阶段推进门禁与 workflow 完成门禁据此判定；
- 对已 `completed` 的任务补落 verdict 同样生效。
- 写入 verdict 的同时，Controller 会自动向 workflow 共享文档区追加一条
  `kind=gate` 的机器证据（source=controller），供下游节点与 stale 判定消费。

### 2.10 `herdr-task note-add` / `note-list`（Workflow 共享文档区）
代码在 CoW clone 中物理隔离，但文档与证据按 workflow 受控共享：追加式账本落
在 clone 外 `~/.herdr-controller/workflows/<workflow_id>/shared/notes.jsonl`。
```bash
# 追加条目（append-only，禁止覆盖历史）
herdr-task note-add --workflow-id <id> --kind <kind> --title "<标题>" \
  [--text "<正文>" | --file <path>] [--node <node_id>] [--task <task_id>] \
  [--agent <name>] [--source agent|human] \
  [--base-sha <sha>] [--round <n>] [--invalidates <node_id>]...

# 查看条目（stale 为读取时计算：base 漂移作废 evidence/gate；fix-loop 作废早于作废点的目标节点条目）
herdr-task note-list <workflow_id> [--node <node_id>] [--limit <n>] \
  [--current-base-sha <sha>] [--json]
```
- `--kind`：`requirement|spec|plan|decision|evidence|gate|invalidation|note|wrapup`；
- `--source`：CLI 仅接受 `agent|human`；`controller` 机器证据由门禁 verdict
  （`herdr-task set ... --verdict`）与 fix-loop 自动落盘，不接受手工伪造；
- `--task <task_id>` 时自动从任务记录与 CoW clone 补全 `node/agent/base_sha`；
- 权威层级（已注入每个节点 prompt）：`git commits / verify-baseline` >
  `controller 机器证据` > `本区文档（仅供上下文，不得当作事实）`；
- 运行时账本根目录可用环境变量 `HERDR_WORKFLOW_DOCS_DIR` 覆盖（默认
  `~/.herdr-controller/workflows/<wf>/shared/`，仅测试/自定义部署使用）；
- 工作流关闭后账本保留供复盘审计，不做自动删除。

## 3. `herdr-preflight` 命令

Agent 本地环境快速检测与准入控制工具。
```bash
# 检查当前项目或所有 Agent 的基础状态
herdr-preflight

# 输出 JSON 格式
herdr-preflight --json

# 临时禁用某 Agent
herdr-preflight --disable pi
```

---

## 4. `herdr-deep-preflight` 命令

沙盒化真实 Provider 探活与深层探针工具。
```bash
# 执行深层探针检测
herdr-deep-preflight --deep

# 执行检测并在发现硬故障时自动剔除
herdr-deep-preflight --deep --auto-disable
```

## 5. `herdr-task compact` 命令

为一个 Run 创建或读取 bounded、append-only 的 Semantic ContextPack。默认只保留引用和 Observation metadata；`--no-model` 使用确定性 fallback，不影响 Task/Workflow/Runtime。

```bash
herdr-task compact --run-id <run_id>
herdr-task compact --run-id <run_id> --task-id <task_id> --json --no-model
```

- `--run-id`：必填的 Trajectory run 标识；
- `--task-id`：可选，用于读取当前 Task goal/status/runtime；
- `--json`：stdout 只输出 ContextPack JSON，诊断写 stderr；
- `--no-model`：不调用 Provider，仍生成程序验证的 facts 和证据引用；
- 相同 `source_event_sequence` 重复调用返回已有 latest ContextPack，不删除历史快照。
