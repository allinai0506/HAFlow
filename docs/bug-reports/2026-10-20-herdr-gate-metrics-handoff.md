# Herdr 流水线门禁与度量缺陷交接单（wf-haflow-1020-02 抢救单实战记录）

- 日期：2026-10-20
- 现场：`wf-haflow-1020-02`（`herdr/state_db.py` 8169 行拆分工程 rebase 移植抢救单）
- 代码基线：`origin/main = f0dfbd8`（本文所有 file:line 均在此提交上重新核对）
- 候选：`b46b81aa9a109d0acbfd0b0a0e4a4cad7b352f63`，位于 `herdr/integration-wf-haflow-1020-02-impl-craft`，**未进 main**
- 症状：implementation 段已完成并冻结候选；review 节点已 `pass`；test 节点两轮均未产出业务放行凭证；**wrapup 节点始终未被派发**
- 交付形态：缺陷清单 + 复现命令 + 最小修复方向 + 测试钩子。不含实现。

---

## 0. 一句话结论

平台有两类结构性缺陷叠加：

1. **凭证面割裂（D-8）**：人工放行原语（`force-pass` / `bypass-gate` / Console signoff）写的是 `tasks.stage_verdict`
   与 `workflow.gate_overrides`；业务汇聚门禁读的是 `events` 表里的 `business_acceptance_recorded` 凭证。
   两套事实源没有交集，导致 **一个 `blocked` 结论的 receipt-v1 门禁任务在文档上不存在任何推进路径**。
2. **失败静默（D-7 / D-9）**：门禁拒推进只打印一行 `[BUSINESS GATE WAIT]` 就 `return False`；
   派发前置失败直接把 Task 写成 `failed`。两者都不升级人工、不记录 blocker、不给出下一步，
   主控只能靠读源码反推"为什么不动"。本次 wrapup 卡死就是这两类叠加的结果。

其余（D-1..D-6）是同一个流水线里已经观察到的相邻缺陷，一并登记，避免下一单再踩。

---

## 1. 当前卡点的因果链（先给可直接验证的事实）

```
候选 b46b81a（base a56739e）
   │  main 已前进 1 个提交 f0dfbd8（纯 console/ 改动，与 45 授权路径 0 交集）
   ▼
test-r2 派发（op 190466）→ herdr/task_cli_launch_preflight.py 判定 baseline_stale
   ▼
Task wf-haflow-1020-02-test-auto-r2: status=failed, stage_verdict="", failure_reason=baseline_stale
   ▼
无 business_acceptance_recorded(pass) 凭证 → business_gate._eligible / has_business_acceptance 拒绝
   ▼
join_gates 持续 [BUSINESS GATE WAIT] → 调度器不放行下一节点
   ▼
wrapup 从未收到 node_dispatch 事件（不是"我漏派"，是"没有事件可答"）
```

已核对的现场数据：

| 项 | 值 | 取法 |
|---|---|---|
| test-r2 状态 | `failed` / `stage_verdict=""` / `failure_reason=baseline_stale` / `integration_mode=none` | `bin/herdr-task get wf-haflow-1020-02-test-auto-r2` |
| 候选落后 main | 1 个提交（`f0dfbd8`） | `git rev-list --count b46b81a..origin/main` |
| 漂移提交重叠面 | 仅 `console/**`，`herdr/state_db.py` 命中 0 | `git diff --name-only a56739e f0dfbd8` |
| review 轮凭证 | `events#97075` verdict=**pass** | sqlite `events` |
| test 轮凭证 | `events#97091` verdict=**blocked**（candidate_sha b46b81a） | sqlite `events` |
| workflow | `wf-haflow-1020-02` status=running；`wf-haflow-1019-02`/`wf-haflow-1020-01` paused | sqlite `workflows` |

**解除本次卡死的最小动作**（顺序敏感，且需要人工授权，主控未擅自执行）：

1. 把 `b46b81a` 重放到 `f0dfbd8`（0 交集 ⇒ 无冲突），得到新 candidate_sha；
2. 用新基线重新 launch test（`--candidate-sha <新SHA>`），旧凭证按 `candidate_sha` 天然失效，无需清理；
3. test 产出 `verdict=pass` 的 `business_acceptance_recorded` 后，wrapup 才会收到 `node_dispatch`。
   不建议再用 `force-pass` 尝试推进——见 D-8，它对 receipt-v1 门禁任务无效。

---

## 2. 缺陷清单

严重度：**P1** = 阻断流水线或产生错误结论；**P2** = 误导决策/损失可观测性；**P3** = 卫生问题。

### D-8 · 人工放行原语与业务放行凭证是两个不相交的事实面（P1，本单主因）

- **位置**
  - 写侧（verdict 面）：`herdr/kernel.py:836 bypass_task_gate`、`herdr/kernel.py:395 force_pass_gate`、
    `herdr/state_db.py:7468 force_pass_gate`、`console/task_api.py:327 api_task_signoff`
  - 读侧（凭证面）：`herdr/task_checkpoint.py:345 business_acceptance_receipt`、
    `herdr/task_checkpoint.py:351 has_business_acceptance`、`herdr/business_gate.py:11 _eligible`
  - 汇聚：`services/controller/join_gates.py:6 _scheduler_join_gate_allows`
- **声称行为**：`herdr/kernel.py:836` docstring —— "Creates a structured bypass receipt, marks `stage_verdict` as 'pass'"；
  Console 审批按钮（`api_task_signoff` approve 分支）在 UI 上表现为"放行本节点"。
- **实际行为**：这些原语只写 `tasks.stage_verdict='pass'` + `gate_bypassed`/`gate_overrides`(+history)，
  **不产生任何 `business_acceptance_recorded` 事件**。而 `_eligible` 与 `has_business_acceptance`
  校验的是 execution_id、candidate_sha、epoch、`required_criteria_sha256`、artifacts 五项一致性，
  且 `payload.verdict` 必须为 `pass`——**没有任何一个 bypass 位参与判定**。
- **后果**：人工已批准、`stage_verdict` 已 pass、`gate_overrides` 已留痕，节点依然不放行；
  主控会误判为"门禁坏了"或去伪造凭证。对 receipt-v1 门禁任务，`blocked` 结论缺少合法推进路径。
- **复现**
  ```bash
  bin/herdr-task force-pass wf-haflow-1020-02 --node test --note "audited bypass"
  bin/herdr-factory step wf-haflow-1020-02        # 仍输出 [BUSINESS GATE WAIT] node=test
  ```
- **最小修复方向**：把 bypass 提升为**受审计的凭证类型**——同一事件面写入
  `business_acceptance_recorded`（或新的 `business_acceptance_overridden`），payload 至少含
  `verdict/execution_id/candidate_sha/epoch/required_criteria_sha256/artifacts/operator/receipt_sha`；
  `has_business_acceptance` 识别 override 凭证并要求 `gate_bypassed=True` 一致。
  二者必须同面，否则禁止 `force-pass` 出现在 receipt-v1 任务的可用操作列表里（fail-closed 比静默无效好）。
- **测试钩子**：现有覆盖面为 `tests/test_node_dispatch_contract.py`、
  `tests/test_scheduler_dispatch_e2e.py`、`tests/test_task_cli_workflow_close.py`（均引用 `has_business_acceptance`/
  `_scheduler_join_gate_allows`），在其中新增
  （a）bypass 后 `step` 放行；（b）伪造 execution_id/epoch 的 override 凭证被拒；
  （c）receipt-v1 任务上 `force-pass` 要么放行要么明确报错，不得"成功但无效果"。

### D-9 · 业务门禁等待只打印不升级（P1）

- **位置**：`services/controller/join_gates.py:15-27`
- **声称**：判定失败/异常一律拒绝（Fail-Closed）——这句是对的。
- **实际**：`print('[BUSINESS GATE WAIT] ...')` 后 `return False`，不 `record_event`、不置 task `blocker`、
  不产生 `waiting_human`、无退避与告警阈值。调度器每轮重跑，形成**无限静默 spin**：
  外表看 workflow=running，实则永不推进。
- **后果**：人类和主控都收不到"需要你"的信号。本单 wrapup 无动作的直接可观测原因。
- **复现**：`tail -f` 控制器日志观察同一 `[BUSINESS GATE WAIT]` 反复出现且无 escalation 记录；
  `sqlite3 ~/.herdr-controller/herdr-state.db "select id,event_type from events where workflow_id='wf-haflow-1020-02' order by id desc limit 20"` 中无 gate-wait 类事件。
- **最小修复方向**：同一 `(workflow, node, reason)` 连续 N 轮或累计 T 秒后，写一条
  `business_gate_stalled` 事件并把 workflow 置 `waiting_human`（带 blocker 文案：缺哪张凭证、缺哪个字段）；
  打印文案带上"缺失项"而不是只有任务列表。
- **测试钩子**：新增 `tests/test_join_gate_stall_escalation.py`：两轮等待不升级、第 N 轮必须产生事件与
  `waiting_human`；异常分支（`OSError/ValueError`）同样计入。

### D-7 · 恢复操作 `fix_loop` 停在 `waiting_human` 且无显式替代要求（P2）

- **位置**：`herdr/node_dispatch_store.py:113 reconcile_workflow`；配套 `services/controller/direct_dispatch.py:221`（`mode == "wait"` 分支）
- **现象**：`workflow_recovery_operations` 中 `190133 fix_loop` 已被 superseded、`190466 node_dispatch` running，
  但 fix_loop 停滞期间没有落 `target_requires_explicit_replacement` 之类的可判定状态，
  主控无法区分"还在重试"和"已放弃等人"。
- **修复方向**：恢复操作进入终态前必须写入拒绝原因与所需替代动作（replacement 的 op 类型/参数模板），
  `reconcile_workflow` 对该状态直接返回"需要人工替代派发"，而不是静默返回既有凭证。
- **测试钩子**：`tests/test_recovery_entrypoints.py`、`tests/test_selective_replan_core.py` 增加
  fix_loop 停滞 ⇒ 断言产出可替代凭证或 `waiting_human`。

### D-2 · 节点已有终态任务后不再发放第二张 `node_dispatch`（P2）

- **位置**：`herdr/node_dispatch_store.py:113-158`（`reconcile_workflow` 的既有操作归属判定）
- **现象**：某节点一旦有 terminal 任务，同节点不会再产生新的 `node_dispatch` 事件。
  本单是靠 `bin/herdr-factory rollback wf-haflow-1020-02 test` + `resume` 才拿到第二张凭证（op 190466）。
- **后果**：需要"同节点重跑一轮"的场景（门禁环境变化、基线漂移、洁净室复跑）只剩 rollback 一条路，
  而 rollback 会连带使下游终态失效，代价与需求不匹配（本次侥幸只影响 test-auto/wrapup）。
- **修复方向**：把"同节点追加一轮派发"作为一等操作（显式 `--dispatch-round`，保留既有 terminal 任务与凭证），
  或在文档中明确"只有 rollback 能重开节点"及其副作用清单。
- **测试钩子**：`tests/test_dispatch_idempotency.py` 增加 round-2 断言：不重复消费 round-1、不撤销 round-1 凭证。

### D-3 · `step exceeds checkpoint budget`（P2）

- **位置**：checkpoint 预算判定（`bin/herdr-factory step` / `herdr/task_checkpoint.py` 预算侧）；
  本单在 S1..S7 分段 checkpoint 时触发。
- **后果**：计划 §6 要求的"短段推进 + 每段落证据"被预算拒绝，段落证据只能落共享台账 `notes.jsonl`，
  机器侧 delivery-check 覆盖不到 ⇒ **不能在结论里宣称"分段 checkpoint 已机器验收"**（本次未宣称）。
- **修复方向**：预算应按"节点/轮次"而非按进程生命周期计；或提供显式 `checkpoint --force-reason`
  并把它计入审计而非报错。
- **测试钩子**：`tests/test_task_checkpoint*.py` 增加预算耗尽场景断言可恢复、不静默丢弃。

### D-4 · 度量基线在派发到工位时就快照，早于任何授权路径 intake（P2）

- **位置**：`herdr/evaluator.py:228 capture_lint_baseline` / `:246 _write_baseline_lint_unlocked`；
  调用点 `herdr/task_cli_launch.py:207 auto_init_task_loop` ← `herdr/task_cli_launch_orchestrator.py:472`
- **现象**：`clones/<task>/.herdr-loop/BASELINE_LINT.json` = `{"lint_errors": 2, "type_errors": 0,
  "captured_at": "2026-10-20 10:43:50"}`，捕获时刻在 45 路径增量合入**之前**；
  之后没有任何再快照入口（`evaluator.py:462` 只有 unlink 清理路径）。
- **后果**：`effective_defects(current, baseline)` 把上游既有诊断算作本工位新增，
  craft worker 只能自行做 fail-closed 归因（本次逐文件 `ruff` 差分证明 +9 全在
  `herdr/state/transition_gate.py` 且逐条与上游 `register_workflow` 同规则 ⇒ 新增 0）。
  即门禁口径要靠工人额外自证才能成立。
- **修复方向**：提供 `herdr-loop rebaseline --reason <授权路径 intake 完成>`（写审计记录 + 保留旧基线 history），
  或在 intake 完成点自动再快照；基线文件加 `baseline_of_sha` 字段以便判定基线归属哪个 sha。
- **测试钩子**：`tests/test_loop_init_baseline_atomicity.py` 同族新增 rebaseline 原子性与旧值留痕断言。

### D-6 / DEF-07 · lint 维度退化为按行数计数，能报 `new=0` 掩盖真实失败（P2）

- **位置**：`herdr/evaluator.py:816 parse_lint_output`
- **机制**：解析失败时回退为统计匹配 `\b(?:error|Error|ERROR)\b` 的行数，且
  `if count == 0 and exit_code != 0: count = 1`。工具崩溃、参数错误、输出格式变更都会变成一个
  语义随机的数字；`METRICS.json` 全零 + `composite_score: 0.0` 即此路径产物。
- **后果**：度量维度无法区分"干净"、"工具没跑起来"、"输出没解析"。D-4 的差分归因也因此不可信。
- **修复方向**：结构化输出优先（`ruff --output-format json`），解析失败必须记为
  `lint_unavailable` 并让门禁 fail-closed，不得回退成计数；`exit_code!=0` 与 `count` 分离记录。
- **测试钩子**：`tests/test_lint_execution_failure_gate.py` 扩展：崩溃/未知格式 ⇒ 断言结论为
  unavailable 而非 0/1 数字。

### D-5 / DEF-05 · `POST /api/project/create` 返回 500 `cli:workspace:create server_not_running`（P2）

- **位置**：Console HTTP 层（`console/http_server.py` + `console/` 下 project/workspace 职责模块）
- **后果**：蓝单 AC-7 的 E2E "project-create" 分支无法走真实产品链路。本单已授权改用
  隔离的一次性 workspace server 承载该腿（R-34），但真实缺陷仍在。
- **修复方向**：区分"服务未运行"（可恢复、应返回 503 + 重试提示）与"CLI 不可用"（500）；
  错误体带实际缺失项（路径/socket/pid）。
- **测试钩子**：Console API 专项断言服务未起时返回码与文案，且不产生半成品 project 行。

### D-1 · `completed` 收编凭证与 `workflows.status` 漂移（P1 类相邻缺陷）

- **位置**：收编/清理写 `completed` 凭证一侧 vs `workflows` 表 status 读取侧
- **现象**：工位已 cleaned、持有 completed 收编凭证，workflow 行状态未同步；
  `bin/herdr-task list` 与 `workflows` 表给出不同世界视图。本单出现 `wf-haflow-1019-02`/`wf-haflow-1020-01`
  停在 `paused`，且 cleaned 的对抗工位无法再产生第二轮复核凭据（⇒ 抢救单 §7 R5 残余风险）。
- **修复方向**：status 迁移与收编凭证在同一事务内写；提供 `reconcile` 修复不一致，
  并把"已 cleaned 但状态未终"作为显式不一致态上报，而不是静默保留。
- **测试钩子**：`herdr-factory reconcile` 专项：注入不一致 ⇒ 断言被检测并给出可审计修复动作。

### D-10（本次新增，登记）· 拒绝派发产生的 `failed` 任务占据节点终态，阻塞下一张凭证（P2）

- **位置**：`herdr/task_cli_launch_preflight.py:176-208`（`baseline_stale_refused` 事件 +
  `record_router_failure(reason="baseline_stale")`）；判定式 `herdr/launch_baseline.py:77-92`
- **先更正主控自己的误判**：拒绝派发时事件载荷**是完整的**——`events#97119` 实测含
  `behind_by=1`、`dev_tip=f0dfbd8…`、`claim_sha=b46b81a…`、`actionable=true`、以及可执行 `remediation`。
  本文初稿称"remediation 没落到事件上"**不成立**，真实情况是主控未查询 `events` 表就下了结论。
- **实际缺陷**：信息在事件里，但任务记录本身是 `status=failed, stage_verdict="", blocker=""`，
  `bin/herdr-task get` 看不到任何下一步；且"从未开始执行"的任务被计为节点 terminal 任务，
  与 D-2 叠加后使"同节点再来一轮"只能靠 rollback（连带撤销下游终态）。
- **修复方向**：区分"任务失败"与"任务从未启动"（后者不占 terminal、不阻塞下一凭证）；
  把 `remediation`/`behind_by` 同步到 task `blocker` 字段；`herdr-task get` 输出 actionable 指针。
- **测试钩子**：`tests/test_dispatch_baseline_failfast.py` 扩展断言：拒绝派发后
  （a）事件含 `behind_by`/`remediation`（已满足，写成回归）；（b）task `blocker` 非空；
  （c）该任务不被算作节点 terminal 任务，下一轮凭证无需 rollback 即可发放。

---

### D-11（重放时新发现）· append-only 文档使每次重放必然冲突，且允许条目编号撞车（P2）

- **位置**：`wiki/log.md`、`docs/lessons/lessons-learned.md`（技能与门禁都要求"只追加"，见 `AGENTS.md` §8 与 `wiki/WIKI.md`）
- **实测**：`b46b81a → f0dfbd8` 的重放只有 1 处冲突（`wiki/log.md`）；main 再前进 3 个提交后，
  同一候选的重放冲突扩为 **3 处**（新增 `docs/lessons/lessons-learned.md`、`wiki/task-lifecycle.md`）。
  其中 lessons 文件出现**编号撞车**：main 与候选各自新建 `## 178.`，git 只能把它切成 3 个交错 hunk，
  无法机械"两块都保留"，必须人工判断顺延为 `## 179.`。
- **后果**：一个纯代码重构的候选，每轮重放都要人工裁决文档冲突；"只差 1 个提交"的真实成本不是 0。
  同时"deletions=0 + main 前缀字节全等"这类不变量目前只能靠临时脚本自证。
- **修复方向**：条目编号由程序按当前文件末尾派生（`## <max+1>.`），或取消全局编号改为日期+slug；
  提供 `herdr-task replay --onto <sha>` 之类的机械重放入口，内置 append-only 三路合并
  （main 前缀保持不变 + 对方追加块顺延），并把 P-09 那套不变量做成门禁。
- **测试钩子**：新增 `tests/test_replay_append_only.py`：两侧各自 EOF 追加 ⇒ 自动合并零冲突；
  两侧新增同号条目 ⇒ 顺延且各自条目可检索；任一 `deletions>0` 直接 fail-closed。

## 3. 修复顺序建议（按能否解锁流水线排）

1. **D-8**（凭证面统一）——不修则任何"人工批准"在 receipt-v1 节点上都是空操作。
2. **D-9**（stall 升级人工）——不修则下一次卡死依旧无人知晓。
3. **D-10**（拒绝派发留可行动信息、不占终态）——本单当前卡点的直接体验问题。
4. **D-4 + D-6**（度量可信度一起修：先有可信 `parse_lint_output`，再谈 rebaseline）。
5. **D-11**（重放冲突与编号撞车——每次基线漂移都在收它的账）。
6. **D-1 / D-2 / D-3 / D-5 / D-7**（一致性与运维面）。

## 4. D-8 修复前的可用性实测（不要重复踩）

四个"看起来能放行"的人工原语，逐一验证后**全部不能**解除 receipt-v1 节点的业务汇聚门禁：

| 原语 | 落点 | 是否产 `business_acceptance_recorded` | 结论 |
|---|---|---|---|
| `herdr-task force-pass`（`herdr/kernel.py:395` → `herdr/state_db.py:7468`） | `tasks.stage_verdict` + `gate_overrides(_history)` | 否 | 空操作 |
| `herdr-task bypass-gate`（`herdr/kernel.py:836`） | `stage_verdict='pass'` + `gate_bypassed` + bypass receipt | 否 | 空操作 |
| `herdr-task adopt-verdict`（`bin/herdr-task:298` → `herdr/task_cli_status.py:266-313`） | 走 `set_task_status(verdict=…)`，与上二者同面；且 `wiki/log.md:174` 限定 `failure_reason == hard_timeout_watchdog` 才允许从 `failed` 流转（本单为 `baseline_stale`，直接拒绝） | 否 | 空操作 / 拒绝 |
| Console `api_task_signoff` approve（`console/task_api.py:327`） | `force_pass_gate` | 否 | 空操作 |

唯一能解除的写点是 `herdr/task_checkpoint.py:340` 的 `acceptance-record`，
其校验（`:351 has_business_acceptance`）要求 `verdict=pass` + execution_id + candidate_sha + epoch
+ `required_criteria_sha256` + artifacts 全覆盖本轮配置标准。
既有裁决（`docs/walkthroughs/20261018-wf-haflow-1018-01-wrapup.md:85`）明确
**回执须由门禁轮次自身落笔，收尾不代签，禁止伪造证据** ⇒ 主控/收尾节点不得替 test 轮签字。

因此 D-8 未修期间的合法路径只有两条：让门禁轮真正跑一轮并自签（§1 程序），
或显式降低本单收单口径（放弃洁净室门禁 AC-G1..G6 或按 blocked 未就绪收口），后者属范围决策，须订单人授权。

## 5. 修这些缺陷时的红线（沿用本单约束）

- 不动 `herdr/state_store.py` 与 `herdr/transitions.py` 契约，不改表结构（新增列需评估 G-7 schema 基线）。
- 跨进程一致性只依赖 db/文件系统契约；内存锁仅声明进程内保证。
- 测试一律临时 db / 临时 HOME / 隔离目录；禁止启动真实 Agent 或调用付费模型。
- 换 `HOME` 做隔离时保留同解释器 `site-packages` 到 `PYTHONPATH`（否则 skip 会被误判为通过）。
- D-4/D-6 属度量语义变更，修完必须重跑本单 G-1..G-7 + GA-1..GA-5 全套并落原文，旧结论不可沿用。

## 6. 台账与凭证索引（供复核）

- 共享台账：`~/.herdr-controller/workflows/wf-haflow-1020-02/shared/notes.jsonl`
  （主控裁决 R-29..R-34；plan 节点 P-00..P-09；craft 段证据 S0..S3）
- 方案正文：`~/.herdr-controller/clones/wf-haflow-1020-02-plan-executor/docs/exec-plans/active/2026-10-20-state-db-rebase-port-plan.md`
- 实现单正文：`~/.herdr-controller/tmp/impl-1020-02-prompt.md`
- 关键事件：`events#97075`（review-auto, verdict=pass）、`events#97091`（test-auto, verdict=blocked）
- 恢复操作：`workflow_recovery_operations` 189889 / 190133（fix_loop, superseded）/ 190466（node_dispatch, node=test）
