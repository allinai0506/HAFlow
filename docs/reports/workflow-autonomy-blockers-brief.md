# HAFlow 工作流自主执行阻塞治理与自愈机制 (HAFlow Workflow Autonomy & Blocker Remediation)

* **Author:** HAFlow Engineering Team
* **Date:** 2026-10-31
* **Subject:** `wf-haflow-1029-02` 真实执行阻塞全链路复盘与系统性自愈方案

---

## 1. 概述 (Overview)

HAFlow 的核心目标是“让人和多个 AI Agent 一起把事情做完（Human + Agent, in Flow）”。在理想状态下，工作流应按照预定义的 DAG 拓扑、节点策略与门禁规则全自动向前推进；仅在触及**业务明确要求人工签署确认的红线**时暂停等待，其余任何非预期的卡点、死锁或静默等待均应被系统内部的调度协调层自愈打通。

然而，对真实执行工作流 `wf-haflow-1029-02`（历时 20 小时、衍生 28 个 Task）的端到端调用链排查证明：当前系统存在严重的**自动化推进断层**。工作流频频被中断并转为“等待人工干预（`waiting_human`）”，使操作人陷入反复诊断、手动执行 CLI 命令解除死锁、手动调度派发的窘境。

本 Brief 旨在以 `wf-haflow-1029-02` 为核心事实锚点，厘清“业务合法等待”与“异常阻塞”的边界，揭示调度器、路由隔离、故障恢复与工位清理四大层面的技术缺陷，确立后续系统性治理的工作假设与目标范围。

---

## 2. 真实案例全链路复盘 (`wf-haflow-1029-02`)

### 2.1 任务执行时间线与关键事件

```
[10-29 21:10] wf-haflow-1029-02 创建 (目标: FIX 启动失败工作流僵尸记录)
     │
[10-29 21:16] requirements-executor (qodercli) ──► PASS (需求规格产出)
     │
[10-29 21:24] plan-executor (qodercli) + plan-challenger (codex) ──► PASS (双角色计划评审完成)
     │
[10-29 22:05] implementation 并行派发:
     ├─ core (opencode)
     └─ console (codex) ──► 22:11 终化报错: fatal: couldn't find remote ref docs/workflow-decision-audit-v1 [阻塞 1: 基线分支缺失]
     │
[10-30 06:38] implementation-core-qoder-r2 完成
     │
[10-30 06:47] test-auto & review-auto 派发 ──► 立即被拒: router_isolation_rejected (qodercli 被 implementation 排除) [阻塞 2: 路由隔离死锁]
     │
[10-30 06:58] 降级/重调派发: test (opencode) + review (grok)
     ├─ review-auto-r2 ──► BLOCKED: 发现控制台展示状态缺陷 (B1)
     └─ test-auto-r3 ──► BLOCKED: 测试验证失败
     │   (注: 门禁发现缺陷触发回炉，属于工作流设计内的预期行为)
     │
[10-30 07:47] 触发 Fix Loop 回炉进入 implementation
     │
[10-30 07:54] implementation-core-remediation-grok ──► 拒派: No available Agent for stage 'implementation' [阻塞 3: 候选池全部耗尽死锁]
     │
[10-30 08:25] 恢复机制触发: op 370043 ──► recovery.dispatch_waiting_human (dispatch_predecessor_changed) [阻塞 4: 恢复状态机主动放弃推进]
     │
[10-30 10:49] 经人工推动后重新测试评审:
     ├─ test-auto-r6 (grok) ──► PASS
     └─ review-auto-r5 (grok) ──► BLOCKED
     │
[10-30 11:15] 恢复机制触发: op 370542 ──► recovery.dispatch_waiting_human (dispatch_scope_cancelled) [阻塞 5: 恢复状态机二次挂起]
     │
[10-30 12:05] 双门禁收敛: test-verify-02617a2 (PASS) + review-auto-r6 (PASS)
     │
[10-30 12:37] wrapup 汇聚节点启动 (opencode ──► r2 ──► r3 qodercli)
     │
[10-30 13:56] 恢复机制触发: op 370566 ──► recovery.dispatch_waiting_human (dispatch_predecessor_changed) [阻塞 6: 恢复状态机三次挂起]
     │
[10-30 14:47] wrapup-auto-r3 ──► PASS (PR #359 建立)
     │
[10-30 16:47] 全流程停顿于总指挥 Pane 提示，等待人工终端执行外部脚本与 close-workflow [阻塞 7: 最后一公里断层]
     │
[10-30 17:14] 人工执行 herdr-task close-workflow wf-haflow-1029-02 闭环
```

---

## 3. 阻塞根因深度剖析 (Deep Cause Analysis)

### 3.1 正常业务决策 vs 异常系统阻塞

* **属于规则约定的正常等待**：
  1. **门禁质量把关回炉**：`review-auto-r2` 审查代码发现“主控制台把 startup_failed 显示成已交付”，以及 `test-auto-r3` 测试未通过。此处的 `stage_verdict: blocked` 是门禁按规则拦截缺陷，触发回炉是设计内行为。
  2. **PR 合并授权确认**：最终代码合入主干前，需人工核对交付物并授权合并（符合“代码可合并并不等于允许合并”原则）。

* **原本应自动推进、却因缺陷中断的异常阻塞**：
  1. **路由隔离策略在回炉（Fix Loop）中的代际耗尽死锁**：
     - *设计依据*：`agent_policy` 规定同一 Agent 不能既当选手又当裁判（如实现者不得直接做测试或审查），以保证工程质量客观性。
     - *实现缺陷*：当前 `herdr/agent_router.py` 简单采用全局历史扁平集合（`stage_used_agents`）。在经历初始实现（opencode, codex, qodercli）以及首轮审查（grok）后，整个 Agent Pool 的 5 个健康 Agent 全部进入排除名单。当回炉进入第二轮甚至第三轮时，系统计算可用 Agent 集合瞬间为 $\emptyset$，触发 fail-closed 强拒派，流程直接冻结。
  2. **恢复机制过度激进进入 `waiting_human` 放弃自愈**：
     - *设计依据*：`workflow_recovery` 的初衷是防止未知或脏状态导致死循环重发。
     - *实现缺陷*：
       - `affected_tasks_unknown`：当审查任务判定 blocked 但未在输出中结构化携带 `affected_task_ids` 时，`herdr/workflow_progress.py` 无法反向归因，直接判为 `waiting_human`，而不是依据当前拓扑或最新 candidate 默认归入源实现节点。
       - `candidate_mismatch`：回炉生成的临时 commit 尚未被 workflow 全局记录时，比对直接不相等，落入 `waiting_human`。
       - `dispatch_predecessor_changed` / `dispatch_generation_changed`：在操作已 started 的情况下，只要前序任务被替换（supersede），调度器不进行就地更新重算，而是立即转为 `waiting_human` 彻底罢工。
  3. **工位清理异常阻断（`unpreserved_worktree_output`）**：
     - *现象*：测试任务产生的 `test-evidence/` 未被纳入受管配置，Teardown 守卫以“存在未提交文件”为由拒绝正常释放工位，导致 clone/pane 资源滞留。
  4. **全自动收尾机制断层**：
     - *现象*：PR 已经建立且自动化测试全绿，Controller 不会依据策略自动将工作流转为收尾归档，而是硬编码要求人工进入 Pane 执行破坏性脚本和 CLI 命令。

---

## 4. 当前系统状态对照 (Status Matrix)

| 阻塞类型 | 具体表现 | 状态分类 | 现状与证据 |
| :--- | :--- | :--- | :--- |
| **远端分支缺失** | `fatal: couldn't find remote ref docs/...` | **已修复** | PR #374 (commit `0b1534c`) 引入 `resolve_integration_base_ref`，预检远端分支并走可重试路径。 |
| **通知丢失整夜静默** | 恢复操作无声等待 9.5 小时 | **已修复** | PR #374 增加了 `_notify_waiting_human` 的 300s 有界退避重试机制。 |
| **Commit 身份冒领** | 他人提交被误收编 (fail-open) | **正在修复** | 本地分支 `herdr/git_adoption.py` 正在增加 `_check_author` 严格校验。 |
| **基线盲区末端引爆** | worktree 陈旧导致最终 commit 拒交 | **正在修复** | 本地分支 `services/herdr-worker.py` 正在增加对 tracking 分支的主动同步。 |
| **路由隔离候选耗尽** | `No available Agent for stage` | **当前仍存在 (阻塞性)** | `agent_router.py` 仍按全局历史粗暴排斥，缺乏多轮次隔离作用域（Round-scoped Isolation）。 |
| **恢复机制自动放弃** | `affected_tasks_unknown` / `candidate_mismatch` 进入 waiting_human | **当前仍存在 (阻塞性)** | `workflow_progress.py` 与 `node_dispatch.py` 逻辑未变，只要匹配失败即刻锁死等待人工。 |
| **工位清理未受管拦截**| `unpreserved_worktree_output` (test-evidence) | **当前仍存在** | 缺少测试产物白名单与自动归档机制。 |
| **末端收尾非自动化** | 必须由人手动执行 `close-workflow` | **当前仍存在** | 缺乏配置化的自动化收敛规则。 |
| **ContextPack 版本失效**| `stale ContextPack source version` | **待验证** | 日志偶发出现，尚未明确是否会直接阻塞状态机流转。 |

---

## 5. 目标与非目标 (Goals vs. Non-Goals)

### In-Scope Goals (本次治理目标)
1. **构建轮次级路由隔离模型 (Round-Scoped Isolation)**：
   - 保证单轮内“实现者与测试者/审查者隔离”的铁律；
   - 在进入后续 Fix Loop（第 2 轮及以后）时，允许已在前期轮次担任测试/审查的 Agent，在提供系统级可追溯审计凭证的前提下，作为新一轮实现的合法候选者，彻底消除因候选池有限导致的逻辑死锁。
2. **修复恢复状态机对自愈的“假死拦截” (Autonomy Recovery)**：
   - 当门禁失败且 `affected_task_ids` 缺失时，建立默认拓扑自愈推导（回退到本阶段所有上游活动实现任务），取消无条件的 `affected_tasks_unknown` 挂起；
   - 解决 `candidate_mismatch` 在修复分支上的时序错配，仅在确实存在不可恢复的身份篡改时才升级人工；
   - 将 `dispatch_predecessor_changed` 改为由调度器自动重新规划（Re-plan/Re-dispatch），而非把运行责任直接甩给操作人。
3. **受管工位与证据目录规范**：
   - 将 `test-evidence/` 等标准化测试产物声明为系统受管忽略/自动上传目录，避免工位清理被误阻断。
4. **工作流全自动终化与收尾状态机贯通**：
   - 梳理 wrapup 后的流转逻辑，使已完成 PR 签署并经校验的工作流能够平滑过渡至归档，无需人工强行干预。

### Out-of-Scope Non-Goals (明确非目标)
1. **不放宽核心安全与隔离边界**：绝不为了图省事而彻底取消跨阶段隔离（如绝不允许同一个 Agent 在同一轮次既写代码又写 Review PASS 裁决）。
2. **不绕过人工授权签署红线**：涉及真实代码推送远端或合入生产分支的高危动作，依然保留人工确认的明确门禁，不盲目追求“无条件静默全自动合并”。
3. **不推翻现有 SQLite + 状态机架构**：完全在既有 `state_db`、`events`、`workflow_recovery` 架构上修补不变量与状态跃迁，不引入平行架构。

---

## 6. 核心决策与演进规划 (Key Decisions & Evolution)

1. **核心决策 1 (已敲定)：采纳方案 A（按轮次隔离，Round-Scoped Isolation）**：
   - 彻底废止全流程单一扁平的 `stage_used_agents` 终身排除算法；
   - 隔离规则的有效作用域收敛至**当前轮次（Current Loop Round）**：在同一轮次中，严格禁止实现 Agent 担任评审/测试；
   - 在进入后续 Fix Loop（第 2 轮及以后）时，允许上一轮曾担任审查的 Agent 作为新一轮实现的执行者，或由独立第三方编写修复，同时生成机器可读的轮次级隔离审计事实。

2. **核心决策 2 (已敲定)：单点精确回炉自愈策略 (Targeted Remediation)**：
   - 彻底修复 `affected_tasks_unknown`：当审查门禁裁决为 blocked 但未携带结构化 `affected_task_ids` 时，Controller 严格依据 Candidate Commit 的溯源链，精确反查出直接产出该 `candidate_sha` 的那个单一实现任务作为回炉修复对象，消除无条件 `waiting_human` 罢工；
   - 杜绝粗暴全量回炉，避免无关并行任务被误牵连打回，保证工程迭代的最小改动集合；
   - 修复 `candidate_mismatch`：明确修复分支生成的新 commit 与工作流全局 candidate 的合法演进契约，消除时序冲突。

---

## 7. 方案 A 的破防式推演与边界质询 (Stress-Testing & Edge Cases)

为保证方案 A 的严密性，需重点防范以下潜在破防场景：

* **质询 1：双 Agent 互换角色自循环风险**：
  若整个项目仅有 2 个可用 Agent（如 A 与 B）：
  - 第 1 轮：A 编码，B 评审并判定 blocked；
  - 第 2 轮（按轮次隔离）：B 接手修复代码。此时第 2 轮的评审者只能是 A。
  - **风险**：A 作为最初写出缺陷代码的人，能否客观、严格地评审 B 针对 A 自己代码所做的修复？是否存在因原有认知偏差导致的“假绿放行”？
* **质询 2：历史审查者跨轮复用规则**：
  - 若第 2 轮修复由新 Agent C 编写，上一轮指出缺陷的 Agent B 是否应当**优先**担任第 2 轮的复验审查者？（从工程效率看，原审查者最清楚缺陷的上下文与重现方式）。
* **质询 3：轮次（Round）在状态机中的权威标识**：
  - 调度器和数据库中当前是依赖 `task.dispatch_round`、`candidate_episode_id` 还是 `fixloop` 状态键作为权威轮次依据？如何保证网络重试或 Pane 重启时不发生轮次倒退？
