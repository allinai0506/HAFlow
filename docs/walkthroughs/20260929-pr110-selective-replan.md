# PR #110 — Selective Replan v1（显式任务定位的返工回路）

> 归档自本 PR 的门禁工件（Entry Gate / S5 验证证据 / S6 独立评审）。
> 原始工件在 `.omc/` 内且被 `.gitignore` 忽略，不随仓库留存；本文件是它们的长期副本。
> PR: https://github.com/allinai0506/HAFlow/pull/110 · 前置：#107 Critical-Path Scheduler、
> #108 Selective Reverification、#109 Controlled Rollout Hotfix

---

## 0. 问题定义

到 #109 为止，HAFlow 已经能回答三个问题：

```text
谁执行？                  → Adaptive Router
哪些节点并行？            → Critical-Path Scheduler (#107)
候选变了哪些验证要重跑？  → Selective Reverification (#108)
```

但缺一个：

> **Verifier 说「有问题」时，implementation 里究竟哪几个 Task 要重做？**

```text
implementation
├─ I-A 后端 API
├─ I-B 前端页面
└─ I-C 数据库脚本
        ↓
test ∥ review
review BLOCKED: 「前端提交失败后没有错误提示」
```

现状是最粗的返工：fix-loop 回流到 implementation，把**整个实现阶段**重做一遍。
一次「前端少了一句错误提示」的评审，会让后端 API 与数据库脚本一起重写——
其中被推翻的概率≈0，而重写的风险与成本却全额支付。

系统里根本不存在 `Blocker → Affected Implementation Task` 这条**事实**，
所以「只重做受影响的」在机制上无从谈起（不是没人想做，是没有承载它的数据）。

---

## 1. 第一原则：Explicit attribution first. Unknown means legacy fallback.

V1 唯一允许触发选择性返工的根据，是 **Verifier 在结构化 Gate Verdict 中明确写下的
`affected_task_ids`**：

```json
{"verdict": "blocked", "note": "前端提交失败后没有错误提示",
 "affected_task_ids": ["wf-123-implementation-frontend"]}
```

明确禁止：LLM 读一句 blocker 猜「应该是前端 Task」；按文件名猜；按模块名猜；
按 embedding 猜；按 CodeGraph / import 图 / AST 猜。**归因只能来自显式字段，
不能来自任何形式的推断。**

无法证明影响范围时**不强行选择性返工**，而是整体回退到既有 fix-loop 原行为。
九个必须回退的条件（缺字段 / 空列表 / ID 不存在 / 跨 workflow / 跨节点 /
已不是当前谱系头 / 已 superseded / 门禁候选身份或版本无法证明 / 结构化结论读不出 /
事实无法持久化）在 §4 逐条给出实测。

**Fail-Closed 的强形式**：不允许部分接受。

```text
❌ 「两个 ID 合法、一个非法」→ 只用合法的两个
✅ 整个 selective decision 拒绝 → legacy fallback
```

理由：被丢弃的那个非法 ID 恰恰是「我们理解错了归因来源」的信号。留下合法的两个，
等于用一条自己都无法解释其完整性的结论去改写工作流。

---

## 2. 方案总览（最小改动面）

```
Gate Verdict JSON（Verifier 写 affected_task_ids）
        ↓  门禁任务元数据持久化
Validate gate evidence → Validate targets（纯函数，全或无）
        ↓
Selective Replan Decision（episode 身份 + plan）
        ↓  持久化为不可变事实（无事实 → 无选择性作废）
supersede 被点名谱系（A/C 零写入）→ 作废旧 test/review/wrapup
        ↓
既有 redispatch pipeline（lineage_redispatch_candidates）
        ↓
I-B-r2（继承原 goal/acceptance/integration_mode/task_type，
        blocker 上下文只进 dispatch prompt）
        ↓
#108 Selective Reverification
```

| 文件 | 职责 |
|---|---|
| `herdr/selective_replan.py` **(新, 528 行)** | 策略解析、`affected_task_ids` 解析、当前谱系头、目标校验、episode 身份、计划构建、Task 清单渲染、replacement blocker 文本、指标纯函数 |
| `herdr/fix_loop.py` (+109) | `verdict_fingerprint` 纳入 targets；`latch_blocks_advance` / `redelivery_handled` 增加 target-aware 分支 |
| `herdr/direct_dispatch.py` (+43) | 门禁契约注入 Task 清单；补派 prompt 注入 blocker 上下文 |
| `herdr/scheduler_facts.py` (+182) | `selective_replan_decision` 不可变事实（复用 events 表，**无新表**） |
| `services/herdr-controller.py` (+568) | 选择性作废编排、awaiting-redispatch、门禁清单注入、selective 通知 |
| `bin/herdr-task` (+43) | `herdr-task set --affected-task-id`（追加式、仅 blocked、PASS+ids 视为非法） |
| `workflow_templates/software-development-v1.yaml` (+13) | `selective_replan:` 显式 opt-in 策略 |

**未修改**：#107 的候选冻结 / Join Gate / `verified_candidate_sha` 身份链、
#108 的 reverification 决策、Adaptive Router、Canary / Rollout 语义、
`herdr/scheduler.py`、stage latch 的 legacy 语义。

未做（§78 禁止项）：requirements / plan 级 replan、任意 DAG 重写、节点级动态依赖重建、
AST / import 图 / CodeGraph / embedding / LLM 影响分析、自动拆 Task、动态新增 Task 类型、
自动调整验收标准。

---

## 3. 关键语义

### 3.1 门禁结论契约升级

Gate Verdict JSON 新增可选数组字段 `affected_task_ids`。CLI 侧：

```bash
herdr-task set <gate-task> --verdict blocked --affected-task-id I-B --affected-task-id I-D
```

- **追加**语义：多次 `--affected-task-id` 累积为数组；
- 只在 `--verdict blocked` 下合法；`pass` + ids 视为非法输入（拒绝）；
- 每次写入 verdict 都**覆写**该字段（含显式空列表）：否则「上一轮点名了 B」会在
  「本轮明确无法归因」之后继续存活，变成幽灵归因。

### 3.2 门禁只能看见当前权威 Task

Verifier 派发时的 prompt 注入 Task Inventory，逐谱系取 `current_lineage_head`
（序号最大的存活成员），**历史 `-rN` 旧版本绝不入清单**——否则 Verifier 会把 blocker
绑到一个已经作废的任务上。清单里列的是「可被点名的 id 全集」，因此 Verifier 点错
的代价是明确的回退，而不是静默的错误重做。

### 3.3 Episode 身份刻意不含 targets

```text
replan_id = sha256(workflow_id, gate_task_id, gate_task_version,
                   gate_verified_candidate_sha, retry_node, policy_identity)[:32]
```

目标是**结论**，不是**身份**。同一个「门禁结论 episode」重复推导必须得到同一个 id，
才能用 `record_event_if_absent` 做到崩溃恢复幂等。若把 targets 编进身份，重放时
任何一次 targets 变化都会生成新事实，幂等性与「以库中事实为权威」同时失效。
代价是「同 episode 换 targets」会撞 id——因此撞 id 时内容不一致必须**拒绝**
（`identity_content_mismatch`），而不是覆盖。

### 3.4 「无持久化 selective 事实，无选择性作废」

顺序被硬编码为：构建 plan → 持久化不可变事实 → 才允许作废。任何一步无法证明
（含 `record_event_if_absent` 返回 rejected / error，或 `exists` 但读不回 payload），
调用方拿到的就是 `None`，走 legacy 精确原行为。**不存在「先作废、后补事实」的窗口。**

### 3.5 保留 = 零写入

`invalidate_for_fix_loop(..., selective_target_task_ids=...)` 只处理被点名谱系的 `-rN`
递增，未被点名的任务**连 status 都不碰**（实测输出 `[SELECTIVE REPLAN PRESERVE]
task=... untouched`）。replacement 的 blocker 上下文通过派发 prompt 注入
（`render_replacement_blocker_note`），绝不回写旧 Task。
`selective_target_task_ids=None` 时函数行为与改造前逐字节一致。

### 3.6 节点必须重新「未完成」，但「重开」不等于「全量重派」

只 supersede B 而 A/C 仍 completed 时 `is_node_complete("implementation")` 为真，
B-r2 永远不会被派发。两处配套：selective 作废时 `clear_stage_advance`；
每轮 sweep 用 `_selective_replan_awaiting_redispatch` 读持久化事实判定
「目标谱系仍有补派候选」，命中即把节点移出 completed 集合并清 stage-advance
（`[SELECTIVE REPLAN AWAIT]`），使节点重新进入就绪流程。

「重开」不会变成「全量重派」：该判定与补派管线 `lineage_redispatch_candidates`
**是同一个谓词**，而 `plan_stage_dispatch` 先判补派候选——有候选时它返回的 specs
只含被点名谱系的 `-rN`。两者同真同假，因此既不提前放行，也不会永久钉住节点。

### 3.7 latch 与重投都是 target-aware

`pending_redo` 与 `fix_loop_item` 新增 `mode` / `target_lineage_roots`：

- `latch_blocks_advance` 要求**每个** target root 在 `latch_ts` 之后都有非 superseded 的
  completed-like 成员（AND，不是 ANY）；
- `redelivery_handled` 要求每个 root 都有 `latch_ts` 之后新建的 `-rN`；
- **被保留任务的时间戳更新永远不能解除 selective latch**；
- 补投（coordinator_busy → `redeliver_pending_fix_loop`）同步保留 `mode` /
  `target_lineage_roots`，不给病理留第二通道；
- `verdict_fingerprint` 纳入 `sorted(affected_task_ids)`：同一条 blocker 先指 B、后指 C
  是两个不同的结论，不是重复。

### 3.8 selective 通知不给全量返工骨架

`build_fix_loop_message` 按 `item["mode"]` 分派。selective 通知列出被点名的谱系根、
明确写出「⛔ 禁止：对 `--stage <retry_node>` 派发全量 fix task」，只给诊断与升级指引，
**不再携带可照抄的 `herdr-task launch` 骨架**。否则 Controller 刚起 B-r2、
总指挥又按同一份通知全量重做实现阶段——这正是本 PR 要消灭的失败模式，
只是换了一条通道回来。

---

## 4. 验收证据

### 4.1 自动化验证（修复轮之后，同一工作区）

```
新增专项        91 passed（herdr/selective_replan.py 65 + controller 26）
定向回归        558 passed, 23 subtests passed in 102.59s
全量            2320 passed, 50 subtests passed in 457.05s   EXIT=0
compileall      EXIT=0
git diff --check EXIT=0
```

基线对照：改动前全量 **2230 passed / 50 subtests / exit 0**，零回归。

**隔离性实测**（§91「测试会写穿实盘注册表」的直接复验）：

- `~/.herdr-controller/stage-state.json` sha1 在定向回归与全量前后**完全一致**：
  `b1f02b4f063d1c34627f6a51e9616815ba8eefdc`；
- 真实 `~/.herdr-controller/state.db` 全表扫描 `workflow_id like 'wf-srp%'` 命中 **0 行**。

### 4.2 真实链路

`tests/test_selective_replan_controller.py` 驱动**真实** `handle_fix_loop` /
`check_workflow_stage_advance`，只拦截派发用的 `subprocess`。实际输出：

```
[SELECTIVE REPLAN]         workflow=wf-srp-ctl gate=review retry=implementation
                           targets=wf-srp-ctl-impl-B
                           preserved=wf-srp-ctl-impl-A,wf-srp-ctl-impl-C
                           replan_id=srd-1f943a6298864cf2eab325e987f0f749
[SELECTIVE REPLAN PRESERVE] task=wf-srp-ctl-impl-A node=implementation untouched
[SELECTIVE REPLAN PRESERVE] task=wf-srp-ctl-impl-C node=implementation untouched
[FIX LOOP QUEUED]          workflow=wf-srp-ctl gate=review retry=implementation loop=1 invalidated=2
[SELECTIVE REPLAN AWAIT]   workflow=wf-srp-ctl node=implementation target lineage pending redispatch
```

**「replacement 建立在保留工作之上」是真实 git 回归证明**，不是 mock：
`test_real_git_replacement_builds_on_preserved_work` 在临时 git 仓库里先让 A/C 落盘
真实文件、B 被 supersede，再由 replacement 派发读取 `context_branch`，断言工作区
**仍含 A/C 的产出**。

**归因唯一来源也被锁死**：`test_verdict_file_is_the_only_attribution_source` 保证
仅在 Gate Verdict JSON 的 `affected_task_ids` 写入时才走了 selective 路径——
`note` 里出现同样的 task id 文本不触发任何 selective 行为。

---

## 5. 评审闭环：一轮独立评审，9 项缺陷

评审者：独立对抗性子代理（Opus 5，read-only，与实现者上下文分离），
verdict **NEEDS_FIXES**，blocking = F1 / F2 / F4（F3 同轮处置）。

| 编号 | 级别 | 缺陷 | 处置 |
|---|---|---|---|
| F1 | MAJOR | 计划按策略声明的 `retry_node` 校验目标，作废却按门禁解析出的 `retry_node` 执行。两者不一致时「保留」过滤器永不命中，会把**未被点名**的任务一并作废，同时留下一条自称 selective 的事实 | 已修：`_resolve_selective_replan` 早期守卫，不一致即整体回退 legacy |
| F2 | MAJOR | `_selective_replan_awaiting_redispatch` 与 `lineage_redispatch_candidates` 对 `superseded_by` 的判断不一致；分歧状态是一个**没有出口的终态** | 已修：awaiting 判定改为复用补派管线同一谓词 |
| F3 | MINOR | sweep 每轮无条件 `clear_stage_advance` 绕开 `notified` 闩，可能退化为每 2s 重复派发 / 重复提示 | 不修，**已验证为良性**（见 §5.1），并新增不变量用例锁死 |
| F4 | MAJOR | `build_fix_loop_message` 对 `mode` 无感知：selective 下总指挥仍被告知全量重做 implementation，与 Controller 刚起的 B-r2 撞在同一条分支上 | 已修：新增 `_build_selective_replan_message`；补投路径同步保留 selective 上下文 |
| F5 | MINOR | `exists` 但 stored payload 读不回时，`isinstance(stored, dict)` 无 `else`，静默把本次重算的 plan 提升为权威 | 已修：回退并打印 `stored_fact_unreadable` |
| F6 | MINOR | 拒绝分支日志打印计划自身 reason，掩盖真正的拒绝原因 `identity_content_mismatch` | 已修：优先 `error` / `reason` |
| F7 | MINOR | `stage_verdict_affected_task_ids` 永不被清除：一次显式「无法归因」的复审仍沿用上一轮归因 | 已修：写入 verdict 时一律覆写（含显式空列表）；替换 1 个旧用例、新增 3 个 |
| F8 | NIT | wiki §6 把该覆盖描述为「清 stage-advance 并跳过」，代码实际是重新入队 | 已修：措辞纠正为「移出 completed 并重新进入就绪流程」，并写明「重开 ≠ 全量重派」 |
| F9 | NIT | AWAIT 循环内逐节点 `load_tasks()`；sweep 路径完全无测试 | 已修：每轮惰性缓存一次；新增两个真实 sweep 用例（正反两向） |

评审来源披露：本轮原本派出三个独立评审者，但本环境的子代理→主会话消息通道失效
（已用一个对照子代理确认 `SendMessage` 无法送达主会话），只有 Opus 5 一路按
「把报告写进文件」的方式送达。其余两路未送达**不是通过**，此处如实披露：
它们的结论在本 PR 中不可用，不得被当作已完成的评审。

### 5.1 F3：不修的理由（用不变量锁死，而非仅靠论证）

评审提出的病理是「每 2s 一次派发尝试 / 每 2s 一次总指挥提示」。实测不成立，
原因可被点名：

1. `plan_stage_dispatch` 先判 `awaiting = lineage_redispatch_candidates(...)`，
   命中即返回 `mode="dispatch"` 且 specs **只含被点名谱系的 `-rN`**，
   永远走不到全量派发分支；
2. F2 修复后 AWAIT 谓词与 `awaiting` 是同一个函数，「节点被重开」与
   「补派管线有候选」严格同真同假，窗口在替代任务落地的下一轮 sweep 立即关闭；
3. 窗口关闭后 `active` 非空 → `mode="wait"` → `mark_stage_advance_notified`
   并返回 True，**不回落总指挥**，也就不存在每 2s 提示。

方向上也成立：即便窗口期内重复入队，`next_replacement_id` 对同一旧 id 给出同一新 id，
补派按任务 id 幂等；而「静默永久死锁」是更坏的终态。

该不变量由 `test_awaiting_window_only_redispatches_targeted_lineage` 正反两向锁死。

### 5.2 变异验证：每个修复必须被一个真会变红的用例守住

方法：把修复点逐条**反向改写**（不是删掉，而是改回评审指出的那个错误行为），
只跑声称守住它的那条用例；预期**变红**。跑完用 `shutil.copyfile` 从
`/tmp/srp-mut/*.orig` 还原，并 `diff` 确认与变异前逐字节一致（见 §5.3）。

| # | 目标修复 | 变异方式（把修复改回缺陷行为） | 守卫用例 | 结果 |
|---|---|---|---|---|
| M1 | F1 策略/门禁 `retry_node` 一致性守卫 | 删去 `policy["retry_node"] != retry_node` 的早退，让不一致继续走 selective | `test_policy_retry_node_mismatch_falls_back` | **GUARDED**（用例变红） |
| M2 | F2 AWAIT 谓词与补派管线同源 | 把 awaiting 判定改回自立的 `superseded_by` 规则（不看谱系是否仍有候选） | `test_dangling_superseded_by_does_not_stall_forever`、`test_awaiting_window_only_redispatches_targeted_lineage` | **GUARDED**（用例变红） |
| M3 | F4 通知按 `mode` 分派 | 让 `build_fix_loop_message` 对 `mode` 无感知，selective 事件退回 legacy 模板 | `test_selective_message_forbids_full_stage_fix_task`、`test_legacy_message_unchanged_for_legacy_items` | **GUARDED**（用例变红） |
| M4 | F5 `exists` 但读不回事实即回退 | 去掉 `isinstance(stored, dict)` 的 `else`，让本次重算结果顶替权威 | `test_unreadable_stored_fact_falls_back` | **GUARDED**（用例变红） |
| M5 | F6 拒绝分支日志优先真实原因 | 把 `reject_reason` 改回只打印计划自身 `reason` | `test_rejection_log_surfaces_identity_mismatch` | **GUARDED**（用例变红） |
| M6 | F7 写 verdict 时一律覆写归因字段 | 改回「仅在 `affected_ids` 非空时写入」，让上一轮归因在显式无法归因后存活 | `test_reverdict_without_ids_clears_previous_attribution`、`test_pass_verdict_clears_previous_attribution`、`test_blocked_without_ids_writes_explicit_empty_list` | **GUARDED**（用例变红） |

6/6 均有真实变红记录，无「写了用例但删掉修复用例照样绿」的情形。
F3 / F8 / F9 不是守卫型修复（分别为「不修 + 不变量用例」「措辞」「性能 + 覆盖」），
不在变异表内。

### 5.3 变异后的工作区完整性

`/tmp/srp-mut/` 只备份了被变异的两个文件（`services/herdr-controller.py`、
`bin/herdr-task`）。还原后：

```
diff /tmp/srp-mut/herdr-controller.py.orig services/herdr-controller.py  → 无差异
diff /tmp/srp-mut/herdr-task.orig         bin/herdr-task                → 无差异
```

即变异验证之后的工作区与验证通过时的工作区**逐字节相同**，§4 的实测数字仍然有效。

---

## 6. 已知边界

- **归因质量的上限就是 Verifier 的诚实度。** 系统能保证的是「Verifier 没点名，
  就绝不猜」；它不能保证 Verifier 点对了。点错 ID 的后果是明确的 legacy 回退，
  而不是静默的错误重做。
- **只覆盖 `software-development-v1` 的 test/review BLOCKED → retry_node=implementation。**
  其他模板、其他 retry 节点一律走 legacy。
- **`gate_task_version` 会随 `store.save_task` 自增**，所以 episode 身份绑定的是
  「该门禁任务的那一版结论」；同一结论被重新落库会得到新的 episode，
  这是有意的（重放幂等由 `record_event_if_absent` 保证）。
- **`herdr/selective_replan.py` 528 行**，略超 `herdr/` 核心 300~500 行梯度阈值；
  同族 `herdr/reverification.py`（713 行）为既存先例 ^[#108]。模块内是单一内聚决策链
  （策略解析 → 目标校验 → episode 身份 → 计划构建 → 清单渲染 → 指标），
  再次拆分会割断该链，故不拆。
- **`services/herdr-controller.py` 由 8105 → 8671 行**，属既存超长服务文件；
  本 PR 已把全部纯决策逻辑下沉到 `herdr/selective_replan.py`，controller 仅保留 I/O 编排。
- **无 Dashboard、无新增只读 CLI 审计子命令**（§83 未要求）。事实可用
  `list_selective_replan_decisions` / `find_selective_replan_decision` 读取；
  §72 允许的指标以纯函数形式提供（`replan_metrics`：
  `implementation_task_count` / `targeted_task_count` / `preserved_task_count` /
  `replan_ratio`），未接任何 UI。
- **仓库无 GitHub Actions / commit status**，`2320 passed` 是本地分支证据，
  不是 CI 强制结果。
- `./bin/herdr-factory doctor` 报 8 条 FAIL，全部为
  `project {nexusarchive,agency-agents,ctx-e2e.AwMlsT,HAFlow} workspace|coordinator`；
  `git stash push -u` 回到未改动状态后**同样 8 条、同样文案**，属无头会话下
  工位/Pane 运行时缓存未落地（CLAUDE.md 坑点 3），与本 PR 无关。

---

## 7. 后续

- **归因来源的进一步收窄**：目前 `affected_task_ids` 由 Verifier 自由填写。
  若要减少「点错」，方向是让门禁 prompt 的 Task Inventory 携带更明确的任务边界
  （例如每条 Task 的 goal 摘要），而不是引入任何形式的自动推断。
- **跨节点归因**不在 v1 范围。requirements / plan 级的选择性重规划需要独立的
  「哪些下游产物承载了哪条上游决定」事实，不能从本 PR 的任务级事实推导。
- **指标接入控制台**（`replan_metrics` 已就绪）是独立改动，需先有指标存储。
- **门禁结论文件的选择循环有两份实现**：`services/herdr-controller.py` 的
  `_verdict_from_file` 与 `_verdict_affected_task_ids` 各自遍历
  `_gate_verdict_file_candidates` 取「第一个给出合法 verdict 的文件」。当前两者谓词
  逐字相同（都是「`dict` + `_normalize_gate_verdict(payload["verdict"])` 为真」，
  且候选顺序一致），所以取值必然同源；但这条不变量只靠两份手写循环维持，
  一旦分歧就会出现「verdict 取自文件 X、归因取自文件 Y」。现有用例
  （`test_verdict_file_is_the_only_attribution_source`）只覆盖字段解析语义，
  不覆盖同源不变量。属评审 `Consider:`（非阻断），后续抽一个共用的
  `_gate_verdict_payload(task)` 把它收敛成单一实现。

---

## 8. 关联

- Wiki：`wiki/dag-workflow-engine.md` §6（选择性返工）、`wiki/log.md`
- 前序：PR #107 Critical-Path Scheduler v1
  （`docs/walkthroughs/PR-107-critical-path-scheduler-v1.md`）、
  PR #108 Selective Reverification v1
  （`docs/walkthroughs/20260928-pr108-selective-reverification.md`）
- 复用而非重写的既有机制：`herdr/direct_dispatch.py#lineage_redispatch_candidates`、
  `next_replacement_id`、`lineage_key`（#61 的「补派按谱系去重」事故教训）
- 教训沉淀：`docs/lessons/lessons-learned.md` §95
- 既有同族教训：§61（补派按谱系去重）、§91（测试会写穿实盘注册表）、
  §93（身份三字段不可顶替）、§94（复用必须被证明）
