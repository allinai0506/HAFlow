# Workflow 故障自愈与收口规范 (Fault Recovery & Close-out Spec)

> **公司：上海共事智能科技有限公司**
> **品牌：共事**
> **产品：HAFlow**
> **一句话：让人和多个 AI Agent 一起把事情做完**
> *Human + Agent, in Flow*
> 本文档规范定义 Workflow 执行过程中六类真实故障的检测、升级、自愈与收口机制。
> 实证来源：`wf-haflow-0923-02` 全流程复盘（23 Task / 504 events / 09-23 21:13 → 09-24 14:08，
> 详见 `wiki/log.md` 对应 wrapup 条目与 `state.db` events 表）。

---

## 0. 分级与落地顺序

| 编号 | 卡点 | 等级 | 性质 |
|---|---|---|---|
| FR-1 | 早熟 DONE（交付前哨兵误报） | P0 | 机制缺失 |
| FR-2 | `blocked` 静默滞留（最耗时） | P0 | 机制缺失 |
| FR-3 | 主仓 WIP 挡住 integrate（exit 5） | P1 | 报错不可行动 |
| FR-4 | 中间版本被提前合入（双 PR） | P1 | 流程纪律 + 工具 |
| FR-5 | escalated 任务静默穿过 close | P1 | 门禁缺失（交付物已就绪，合入即得） |
| FR-6 | 测试用了实现 Agent + 测错分支 | P2 | 默认策略不收敛 |

落地顺序：FR-5（合入 PR #94 即得）→ FR-1 + FR-2（同属 Sentinel/Controller，一批做）→
FR-3 → FR-4 → FR-6（路由默认策略收紧）。

---

## 1. FR-1 早熟 DONE：完成信号必须与交付证据联动

**现象（实证）：** impl-fix4、review-t2、wrapup-t1 三次 `HERDR_CONTROLLER_DONE_EVENT`
到达时 agent 仍为 `working`、notes 为空，靠人工 `set rework` 纠正。

**根因：** `services/herdr-sentinel.py:344` 完成判定只检查 pane 可见文本含
`HERDR_TASK_DONE:<task_id>`，不看 agent 是否真正 idle（同文件 nudge 路径有
`agent_status()` 检查，完成路径没有），且单次命中即翻转，无去抖。

**产品需求：**

- FR-1.1 完成三联条件：marker 出现 **且** `agent_status == idle` **且**
  连续两次轮询（间隔 ≥ 1 轮询周期）marker 稳定存在，三者齐备才允许
  `working/dispatched → agent_done`。
- FR-1.2 marker 出现但 agent 非 idle 时，记 `early_done_signal` 计数并写事件，
  不翻转状态；累计 ≥ 3 次则向 Coordinator 发提示（疑似 agent 误标完成）。
- FR-1.3 验收：在复盘三任务同等 dedicated 复现下零误报；正常完成延迟增加
  ≤ 2 个轮询周期。

---

## 2. FR-2 blocked 滞留：升级必须有 SLA，不能静默过夜

**现象（实证）：** test-t1 滞留约 8 小时、test-t1-r2 56 分钟、impl-t4/test-t5 各
1 小时；`inner_loop_exhausted → blocked` 后无任何自动动作，全靠人路过推一把
（`blocked → working` 均为 `cli_set_status` 人工触发）。

**根因：** `services/herdr-sentinel.py:353` 置 blocked 后责任即转移；
Controller 的 `inner_loop_exhausted` 专属仲裁卡（`herdr-controller.py:2079`）
只在 Coordinator 被唤醒时才送达；`herdr-notifier.py` 虽有
`ATTENTION={"blocked",...}` 通道，但 blocked 无超时升级语义。

**产品需求：**

- FR-2.1 blocked SLA：`inner_loop_exhausted` 类 blocked 超过可配置阈值
  （默认 30 分钟）仍无 Coordinator 动作 → 自动用 BLOCKER.md 摘要
  `herdr agent prompt` 重推工位一次（附带"第 N 次自动重推"标记）。
- FR-2.2 自动重推后仍无进展（再超 1 个 SLA 周期）→ notifier 最高优先级
  通道升级给人类（含 BLOCKER.md 摘要 + 一键 supersede / rework 指令）。
- FR-2.3 验收：复盘四次滞留场景下，人工介入前等待时间 ≤ 2×SLA；
  `state.db` 可查每次自动重推事件。

---

## 3. FR-3 主仓 WIP 挡住 integrate：报错必须可行动

**现象（实证）：** `bin/herdr-task:3344-3359` 主仓有已跟踪未提交改动 →
integrate 直接 `exit 5` 中止；用户侧感知为"流程报错"但不知谁该动、
动什么。

**根因：** 检查点只有"中止"，没有"谁来解、怎么解、在哪看"；且检查发生在
integrate 执行时，而非可提前预警的派发时。

**产品需求：**

- FR-3.1 exit 5 输出必须含三要素：挡住的文件清单（前 10 个 + 总数）、
  归属判断（`git stash list` / 当前分支提示"这是你的 WIP，不是任务产物"）、
  精确解法（提交/stash 后重跑 `herdr-task integrate <task>`，由 Controller
  自动重试）。
- FR-3.2 `launch --integration-mode git` 派发时预检主仓是否干净；脏则
  warning（不阻断派发，阻断点仍在 integrate），让用户提前知晓收口会被挡。
- FR-3.3 验收：任意 WIP 下触发 integrate，报错行 30 秒内可定位到责任人与动作。

---

## 4. FR-4 中间版本提前合入：交付身份必须唯一且可校验

**现象（实证）：** PR #92（head=`adf8a32`，未经最终 test/review）在 09-24 10:21
被手动合入 main；wrapup 建 PR #94（head=`ce7e3cd`，含最终候选 `33a1f1d`）时
形成双 PR 局面。

**根因：** 候选身份（branch + sha + 评审结论）只存在于 Coordinator 上下文，
GitHub 侧无约束；人工可在任意时刻合入任务分支的任意中间态。

**产品需求：**

- FR-4.1 review-pass 落盘时写 workflow 级交付记录：
  `delivery_branch / candidate_sha / review_task / test_gate`（与 gate-verdict
  同目录），作为全链路唯一交付身份。
- FR-4.2 wrapup 建 PR 前校验：head 必须 fast-forward 包含 `candidate_sha`，
  且 base 上无同分支已合入 PR 覆盖更新内容；否则 abort 并给出差异说明。
- FR-4.3 PR 描述模板自动注入四要素（branch/head-sha/base/review 结论链接），
  合入者 10 秒内可判断"这是最终版还是中间版"。
- FR-4.4 验收：复盘场景下，PR #92 式提前合入会被 FR-4.2 规则显式警告。

---

## 5. FR-5 escalated 任务穿过 close：收口必须显式认领

**现象（实证）：** impl-fix1（`committed` + `finalize_escalated:
integrate_rebase_conflict`）在 14:08 的 `close_workflow` 中被静默放行
（`forced=false`）；`unsettled_git` 门（`bin/herdr-task:3922`）明确排除
escalated 任务，无第二道闸。

**根因：** escalated 语义是"待人类显式确认"，但 close 路径没有对应的确认点。

**产品需求：**

- FR-5.1 close 增设 `escalated_git` 闸：存在 `finalize_escalated` 非 superseded
  任务 → `[CLOSE ABORT]` exit 2，要求 `--accept-escalated` / `--force` /
  `--abandon` / 先 `supersede` 四选一显式认领（实现即本次候选 `33a1f1d` 已含
  的门禁逻辑——**合入 PR #94 即得，PR URL 见 wrapup 报告**）。
- FR-5.2 close 成功报告必须单列"escalated 但被认领放行"任务及其认领方式，
  不得混在普通 cleaned 行里。
- FR-5.3 验收：复盘终态重放 close（不带认领 flag）必须 abort 并点名 impl-fix1。

---

## 6. FR-6 测试隔离：默认策略必须 fail-closed

**现象（实证）：** test-t2 被 Router 选了 `opencode`（实现主 Agent），且三轮
测试均未覆盖候选分支，最终判全流程唯一 `failed`。

**根因：** `herdr/agent_router.py:286` 的 `exclude_stage_agents` 是 opt-in
策略（workflow 未配即不生效）；test 节点无"基线必须含候选"的硬校验。

**产品需求：**

- FR-6.1 路由默认 fail-closed：test/review 节点自动排除 implementation
  节点用过的 Agent（相当于全局默认 `exclude_stage_agents`），workflow 需
  显式 opt-out 才能放宽，且 opt-out 写事件留痕。
- FR-6.2 test 派发时校验 `baseline_commit` 必须可达当期候选（有交付记录时
  对 FR-4.1 的 `candidate_sha`），否则 launch 直接拒绝并说明。
- FR-6.3 验收：复盘 test-t2 派发条件重放，Router 不得再选 opencode；
  基线不含候选时 launch 拒绝。

---

## 7. 回归要求

- 六项全部带专项测试（test-t2/fix3/早熟 DONE 三场景必须有 deterministic
  复现用例，参考 `tests/test_impl_fix4_regression.py` 模式）。
- `pytest -q` 全绿 + `ruff` 新增 0 是合入前提（沿用本工作流验收线）。
