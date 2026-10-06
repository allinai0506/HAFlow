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

## 6. `herdr-task canary-eval` 命令

Adaptive Router v2 Canary 只读评估（`shadow-eval` / `route-shadow` 为同类只读路由命令，
详见 `docs/architecture/adaptive-router-canary.md`）。对比同一白名单 bucket 内
diverted（Adaptive arm）与 non-diverted（Legacy arm）两组真实执行的 settled Outcome：
qualified success / wall time / rework / blocked / human intervention / ETQS 近似。
只报 observed 事实，不输出是否扩大流量的结论（#104 为人工决策）。

```bash
herdr-task canary-eval [--node <node>] [--task-type <type>] [--agent <agent>] \
  [--since 7d] [--limit 1000] [--json]
```

- `--node` / `--task-type` / `--agent`：按 bucket 维度过滤决策行；
- `--since`：仅评估该时间之后的决策，`Nd` / `Nh` 后缀或 epoch 秒；
- `--limit`：最多返回 N 条匹配决策（寻找匹配可能扫描更多历史，有 scan_cap 上限）；
- `--json`：输出机器可读报告（coverage / buckets / delta / collection meta）。

Canary 运行时配置（默认关闭，无配置文件即关闭）：

```json
{
  "enabled": true,
  "percentage": 5,
  "buckets": [
    {"agent": "codex", "node": "implementation", "task_type": "fix",
     "percentage": 50}
  ],
  "admission_scan_cap": 500
}
```

- 配置路径：`~/.herdr-controller/route-canary.json`，可用环境变量
  `HERDR_ROUTE_CANARY_CONFIG` 覆盖（测试/隔离环境）；
- `percentage`：全局确定性分流比例（1-100），bucket 内 `percentage` 可覆盖（#104 扩量旋钮）；
- 准入：bucket 的 `model_data_status` 与 `evaluation_data_status` 必须同时
  `sufficient`（复用 Shadow Evaluation 权威判定），否则该 bucket 不分流；
- 分流：`sha256("canary-v2|{run_id}|{task_id}")` mod 100 < percentage，跨进程可复现；
- 回退：Canary 路径任何异常 fail-open 到 Legacy Router 并留下 `route_decision_error`
  （mode=canary）审计事件。

## 7. `herdr-task rollout` 命令

Adaptive Router Controlled Rollout（详见
`docs/architecture/adaptive-router-rollout.md`）：per-bucket
`recommended_agent × node × task_type` 独立阶段 `off/5/10/25/50`，人工推进、
可审计、可回退；系统不自动扩量，只可自动止损。

```bash
herdr-task rollout status [--json]
herdr-task rollout set --agent codex --node implementation --task-type fix \
  --percentage 10 --reason "reviewed canary results"
herdr-task rollout off --agent codex --node implementation --task-type fix \
  --reason "manual rollback"
herdr-task rollout history [--agent codex] [--node implementation] \
  [--task-type fix] [--limit 100] [--json]
herdr-task rollout check-guard --agent codex --node implementation \
  --task-type fix [--auto-rollback]
```

- `status`（只读）：列出有 staged 行的 bucket 与当前阶段；kill 时标注 KILLED；
- `set`：相邻推进（`off→5→10→25→50`），跨级拒绝（exit 2），`--reason` 必填；
  百分比先校验后转换，`5.9`/`NaN` 等非精确整数输入直接 exit 2，绝不截断；
- `set` 对**没有 staged 行**、正由 canary 配置服务 N% 的 bucket 执行 `set N` 时
  记 `action=takeover`（输出 `5% -> 5% action=takeover`）：流量不变、所有权
  迁移到 staged 行，此后才能继续 `set 10`；只有「行已存在且值相同」才是真 no-op；
- `off`：从任意阶段直接回 `off`，`--reason` 必填；
- `history`（只读）：`rollout_audit` newest-first，每次变化恰好一条；
- `check-guard`：只读评估 Safety Guard（复用 canary-eval 两臂 facts，证据窗口
  从本 bucket 最近一次阶段变更起算 —— 回退后重试会重新累积证据），
  `--auto-rollback` 触发时持久化 `→off`（`action=auto_rollback`）；
- `set` / `off`：非法阶段或开放百分比、跨级推进、缺 `--reason`、并发冲突
  一律 exit 2（状态未变）；存储失败 exit 1（无部分生效）；
- bucket 身份是复合主键 `(agent, node, task_type)`，两个不同 bucket 不会共享一行；
- stage 落在 off/5/10/25/50 之外时只有 `off` 可用，且 `off` 会写入修复腐坏行；
- 并发保护：CAS 覆盖「行是否存在 + 百分比」，紧急回退不会被读到过期的 promotion
  覆盖，no-op 同样校验（不会从过期读报告 “already off”）；
- 读写共用同一个 DB 解析器，不会出现 set 写一个库、status 读另一个库；
- `off` 对**没有 staged 行**的 bucket 会写入显式 `0` 行来覆盖 canary 配置
  fallback（absent ≠ explicit off），否则该 bucket 仍按配置分流；
- `status` / `history` / `check-guard` 走只读连接，不建表不迁移；读取失败
  返回 exit 1 并报 `unavailable`，不会显示为“全部 off”；
- `check-guard` 在 `status=unavailable`（评不了）时退出 1，JSON 写 stderr；
  样本不足（`insufficient_samples`）属正常判断，退出 0；
- Kill：`HERDR_ADAPTIVE_ROLLOUT_ENABLED=false` 立即全 bucket Legacy，历史保留；
- 热路径 guard 默认关闭，需要时用 `HERDR_ROLLOUT_HOT_GUARD=1` 开启；
  自动止损默认由 `rollout check-guard --auto-rollback` 显式执行。


## 节点配额、原位返工与工位回收

`agent_policy.max_concurrency` 限制 pending/活跃任务，节点 `max_tasks_per_node` 限制全部历史任务。launch 在工作流级跨进程锁内检查，拒绝时列出已有 task_id。替换需要真实空闲并发槽位；满额时先使用同任务 rework。默认软件开发模板累计配额：需求 2、计划 2、实现 12、测试 4、评审 4、收尾 1。

```bash
herdr-task panes --workflow-id <wf> --json
herdr-task rework <task_id> --prompt "<阻断项和验证要求>" --reason "review blocked"
herdr-task reap --workflow-id <wf>             # 只预览
herdr-task reap --workflow-id <wf> --apply     # 身份验证通过后回收
```

panes 输出每节点累计 Task、活跃 Task、去重工位引用及上限。引用数来自持久状态，不等于已证实在线的工位数。Console 节点和 Controller 面板常驻相同计数，越线高亮。

旧 `max_agents` 保留派发模式兼容；累计派发超过其数值阈值时，需要显式 `launch --ack-overflow`，并在路由前写入 `node_overflow_acknowledged` 审计事件。新硬配额不允许确认越过。例外替换必须提供 `--supersedes <task_id> --supersede-reason "<原任务无法继续的具体理由>"`；完成替代任务登记和投递后才退役旧任务。

rework 仅允许可继续的非终态任务，验证当前工位实例身份，保留 task_id/run_id/pane_id，清除旧门禁结论。投递失败留下 pending 意图，可在原任务重试。Controller 使用 `--request-id` 去重同一门禁请求。历史终态不复活。

failed/superseded 等归档任务的工位引用在状态转换事务内标记 orphaned。reap 仅回收身份已确认的动态私有工位，保护预建工位、节点锚点、协调器及其他活跃任务引用；未知身份保留。物理关闭成功但状态写回中断时，后续以 pane_missing 证据补齐释放记录，不删除 clone。

`herdr-task supersede <task_id> --reason "需替换的原因"` 保留待替换义务；用 `--by <replacement_task_id>` 链接同一 Workflow、同一节点的实际替代者，已知跨节点链接在写入前拒绝，未登记/未交付的替代者不会使节点完成。明确移除任务范围使用 `supersede <task_id> --abandon --reason "范围变更的原因"`，不与 `--by` 同用。放弃不绕过节点 `required_task_ids`；全部任务放弃的节点保持未完成、等待范围裁决。无新字段的历史记录兼容旧完成口径，恢复存量 Run 应显式补充必需清单。

## 工作流可靠性：完成回执、检查点与交付投影

以下入口对应本分支实现；工作树验收不代表当前安装服务已经加载。新任务的启动提示提供受管凭据路径及 task/run/epoch，历史无协议任务保留既有兼容行为。

```bash
# 本轮执行结束的声明，不代替测试、门禁、合并或生产验收。
herdr-task report-completion <task_id> --identity-file <private_identity.json> \
  --artifact <observation_id>:<sha256>

# 由控制器显式续签；同一 operation-id 用于崩溃恢复，不用于新一次续签。
herdr-task renew-completion <task_id> --operation-id <stable_renewal_id>

herdr-task checkpoint-publish --task-id <task_id> --run-id <run_id> --epoch <epoch> \
  --segment-file <report_segment.txt> --step 1 --next-step '<下一步>'
herdr-task checkpoint-read --task-id <task_id> --run-id <run_id> --epoch <epoch>
herdr-task checkpoint-aggregate --task-id <task_id> --run-id <run_id> --epoch <epoch>

herdr-task tool-run --task-id <task_id> --run-id <run_id> --epoch <epoch> \
  --timeout 60 --output-limit 65536 -- <program> <args>

# 默认只读；--apply 只释放已证明资源不存在的启动意图，不回收未知/外来资源。
herdr-task launch-reconcile --workflow-id <workflow_id> --node <node_id> \
  --dispatch-role <role> --candidate-sha <full_sha> --dispatch-round 1

herdr-task delivery-report <workflow_id>
```

完成凭据由服务端限定 24 小时；续签保留 epoch/检查点，轮换 bearer 并撤销旧凭据。原操作已开始传输但没有成功回执时保留 unknown，不能换一个 ID 盲目重发。关闭/重开与提示发送共享工作流生命周期锁，发送前重新核验当前任务与原生实例。

检查点只接受当前 task/run/epoch、受管路径及完整哈希。最多 1000 分段，单段 1 MiB、聚合 8 MiB；已发布文件被改写时拒绝恢复。tool-run 使用临时 HOME、移除状态/投影路径，防默认数据库误写；超时退出 124、输出预算中断退出 125，均记录副作用 unknown。此隔离不替代操作系统沙箱，工具若依赖用户配置或认证，需明确的受控契约，不能隐式继承操作员环境。

交付投影区分 observed_result 和当前候选可采用的 status；源码变动、旧 epoch、未知执行方式或失效产物均不能复用旧绿色验收。任务历史超过投影预算时 tasks_truncated=true，all_verifications_passed=false。无部署或生产回执时相应状态始终 unknown。

节点应明确 artifact_mode=repository_changes 或 shared_artifacts；后者要求 integration_mode=none。旧纯报告 wrapup/docs/git 缺少显式模式会给出可恢复错误；文档需要 Git 提交时显式选 repository_changes。

### 有证据迁移、预算增加与业务回执

`herdr-task workflow-migrate <workflow_id> --action plan --output <新文件>` 只读生成计划；review 后以 `--action apply --input <计划> --output <新回执>` 做 CAS 迁移。`--action rollback --input <回执>` 只回滚未被后续写入改变的迁移字段。禁止根据文件名猜测历史 Run；审计不复制 Task 任意内容。

`herdr-task node-budget-extend <workflow_id> <node> --expected-sha <配置哈希> --additional <1..8> --operator <操作者> --reason <理由>` 显式增加累计上限，总上限 64，保留历史计数。配置哈希来自当前固定配置，不是候选 Git SHA。

`herdr-task checkpoint-read --task-id <Task> --run-id <Run> --epoch <epoch>` 返回已登记片段及配置的 AC-N。`herdr-task acceptance-record --task-id <Task> --run-id <Run> --epoch <epoch> --candidate-sha <40位SHA> --verdict pass --artifact <observation_id:sha256> --criterion AC-1=pass ...` 必须覆盖全部配置标准，绑定本轮片段。通用评分和执行测试结果不会自动生成业务 PASS；实际 artifact 语法以 `--help` 为准。
