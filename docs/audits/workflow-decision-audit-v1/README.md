# HAFlow 节点决策逻辑统一性审计报告 V1 (Summary & Index)

> **审计项目**：HAFlow 节点决策逻辑统一性审计 V1
> **审计对象**：`software-development-v1` 标准 6 阶段软件开发与评审流程
> **审计基线**：GitHub `allinai0506/HAFlow` 主干（Commit `739ef0d`）
> **审计周期**：2026-10-29
> **审计结论级别**：基于真实源码、测试断言与现场运行证据确认

---

## 报告文件索引

本审计报告由 6 份专项文档及本篇总览组成，各文档完整路径如下：

1. **真实执行链还原**：[`docs/audits/workflow-decision-audit-v1/execution-chain.md`](execution-chain.md)
   *覆盖：软件开发流程 6 阶段真实调用链、模块与函数映射、权威持久化地图、同步/异步节拍。*
2. **决策规则全量提取**：[`docs/audits/workflow-decision-audit-v1/decision-inventory.md`](decision-inventory.md)
   *覆盖：15 项关键生命周期规则清单，以及 8 种异常决策反模式的代码级对照。*
3. **统一决策表与状态一致性**：[`docs/audits/workflow-decision-audit-v1/decision-table.md`](decision-table.md)
   *覆盖：Slack 式归一化决策模型、10 大场景预期 vs 实际决策矩阵、六大核心不变量审计、5 种持久化事实源一致性分析。*
4. **真实复杂性来源与缺陷诊断**：[`docs/audits/workflow-decision-audit-v1/complexity-findings.md`](complexity-findings.md)
   *覆盖：F-01 至 F-07 共 7 项具体缺陷，附代码位置、触发条件、复现证据与最小修复。*
5. **场景化验证与取证**：[`docs/audits/workflow-decision-audit-v1/validation-results.md`](validation-results.md)
   *覆盖：9 大核心场景实测矩阵、2 个硬性崩溃现场取证记录。*
6. **最小化改进与修复方案**：[`docs/audits/workflow-decision-audit-v1/remediation-plan.md`](remediation-plan.md)
   *覆盖：架构改造评估（无需新建引擎）、P0/P1/P2 级改进路线。*

---

## 核心问题权威回答 (Q1 ~ Q6)

---

### Q1：HAFlow 当前节点推进是否具有确定性？

#### 结论：**核心 DAG 纯逻辑确定，但端到端执行链严重缺乏确定性**。

- **已确认事实**：
  1. [`herdr/workflow.py`](../../../herdr/workflow.py) 中的 DAG 纯函数（`get_ready_nodes`、`is_workflow_completed`）具备 100% 确定性与单调性；
  2. 但端到端推进受制于多个**非确定性外部变量**：
     - **时序竞争非确定性**：在 `plan` 阶段，系统同时并发派发 `executor` 与 `challenger`。两者的生成耗时微秒级差异决定了是“顺利通过”还是被判定为 `challenger_outdated`（时间戳倒挂）或 `challenger_plan_ref_missing`（缺少 Plan-Ref 引用）而遭遇拒绝；
     - **自然语言匹配非确定性**：Controller 使用硬编码中文正则做需求与计划段落判定（[`herdr/stage_skills.py:421`](../../../herdr/stage_skills.py#L421)）。Agent 输出语义相同但用词稍异（如写英文或使用同义词）会导致完全不同的推进决策。
- **证据**：
  - 源码：[`herdr/direct_dispatch.py:898-933`](../../../herdr/direct_dispatch.py#L898-L933) 与 [`herdr/plan_parser.py:549-570`](../../../herdr/plan_parser.py#L549-L570)。
  - 测试复现：修改输入文本格式后，`validate_requirements_deliverable_content()` 结果在通过与失败之间波动。
- **未验证的部分**：真实生产环境下不同 LLM 模型（Claude 3.5 Sonnet vs GPT-4o vs OpenCode）在不同系统负载下的并发耗时分布。
- **建议行动**：
  将 `plan` 节点的 `executor` 与 `challenger` 调整为因果先后派发；将中文硬编码正则扩展为标准词表及支持 YAML Frontmatter 结构化数据解析。

---

### Q2：是否存在重复、冲突或互相阻塞的决策逻辑？

#### 结论：**存在大量重复判断、局部竞态冲突以及多处静默死胡同**。

- **已确认事实**：
  1. **重复判断**：`_scheduler_join_gate_allows()` 在一次推进链路中，分别在 [`advancement.py:323`](../../../services/controller/advancement.py#L323)、[`stage_delivery.py:64`](../../../services/controller/stage_delivery.py#L64)、[`direct_dispatch.py:81`](../../../services/controller/direct_dispatch.py#L81) **被无缓存连续重复计算 3 次**；
  2. **竞态冲突**：Sentinel 看门狗判定超时将任务置为 `failed` 与 Controller `process_gate_verdict_reconciliations` 尝试从磁盘读取有效门禁将其跃迁为 `completed` 存在并发竞争；
  3. **静默死胡同 (Silent Deadlock)**：在 `task_events.py:53-64` 中，Staged 流程在自动验收失败后直接 `return` 且不再重试，任务被永久锁定在 `agent_done`。
- **证据**：
  - 代码位置：[`services/controller/task_events.py:48-64`](../../../services/controller/task_events.py#L48-L64)。
  - PR #356 代码审查历史及 Lesson 208 记录。
- **未验证的部分**：高并发极端压力下 SQLite 数据库连接锁争用对 `claim()` 租约的具体延迟放大倍数。
- **建议行动**：
  在 `task_events.py` 中为自动验收失败增加带有限额的自动 `rework` 跃迁；将汇聚门禁判定收敛至 `advancement.py` 单点求值。

---

### Q3：当前人工介入多，主要是 Agent 执行失败、交付验收失败，还是编排决策失败？

#### 结论：**主要责任在“编排决策失败与验收反馈断链”，其次是“交付验收规则脆弱”，而非“Agent 执行能力不足”**。

- **已确认事实**：
  通过对真实代码与测试现场的追踪，发现导致人工介入的核心诱因主要呈现以下分类特征（注：具体宏观分布取决于不同项目的任务特征，以下为基于机制分析的诱因分类）：
  1. **编排决策断链 (Confirmed & Reproduced)**：
     PR #356 为了去掉常驻 LLM 总指挥（Coordinator），在 `task_events.py` 中引入了 bypass 逻辑。**但只做到了“不再唤醒总指挥”，却没有补上“由代码驱动自我修复/返工”的闭环**。一旦校验不通过，编排引擎直接撒手不管（直接 `return`），任务停留在 `agent_done`，迫使人工必须介入使用 CLI 手动推进状态；
  2. **交付验收规则脆弱 (Confirmed)**：
     Agent 本身已完成交付物编写或代码修改，但 Controller 的验收条件依赖硬编码正则，或要求 Challenger 在非结构化文本中精准匹配格式；格式轻微失配即被机器判定为“交付失败”；
  3. **Agent 执行本身失败 (Covered by Existing Gates)**：
     代码报错、编译失败或真实逻辑缺陷在真实场景中确实存在，但这部分已被现有的 `fix-loop` 与测试门禁机制所识别并管理。
- **证据**：
  - 测试事实：在 `tests/test_software_development_v1_e2e_isolated.py::test_03` 中，当提供符合预期规范的结构化输入时，多任务拆分与门禁评估能够在核心层顺利打通。
- **未验证的部分**：在极其复杂的真实业务代码重构场景下，Agent 代码生成的单次成功率基线数据。
- **建议行动**：
  闭合编排层的自动返工反馈环（给 Agent 提示错误让其重写）；降低交付验收对非必要格式标记的刚性要求。

---

### Q4：现有恢复机制是否存在重复恢复、无限循环或无效重试？

#### 结论：**门禁返工受预算控制良好（无无限重试），但超时恢复与死区重刷存在局部无效空转**。

- **已确认事实**：
  1. **有效受控部分**：[`services/controller/fix_loop.py`](../../../services/controller/fix_loop.py) 设有强约束 `FIX_LOOP_MAX = 3` 与结论指纹去重（`repeat_verdict`），门禁阻断打回在 3 次耗尽后能确定性停止并升级人工，不存在门禁级的无限返工循环；
  2. **无效空转部分**：
     - 当任务处于 `agent_done` 且进入 F-01 的旁路后，`registry_watcher` 每 2 秒调用一次 `redeliver_done_event()`，每 2 秒重复打印一次 `[STAGED TASK EVENT BYPASS]`，形成无效的日志刷屏与空转；
     - `stage-state.json` 文件锁若残留 `notified` 状态，Controller 每 2 秒都会在推进时判定“节点已通知”，跳过所有就绪节点，工作流处于无声假死。
- **证据**：
  - 代码位置：[`services/controller/registry.py:240-242`](../../../services/controller/registry.py#L240-L242) 与 [`herdr/fix_loop.py:120-180`](../../../herdr/fix_loop.py#L120-L180)。
- **未验证的部分**：超长周期（数天）未关闭的历史工作流在磁盘上产生的遗留 `attention.json` 对 Controller 内存占用的长期膨胀率。
- **建议行动**：
  对 `redeliver_done_event` 施加最大重试阈值；将 `stage-state.json` 文件锁收敛至 SQLite。

---

### Q5：`software-development-v1` 能否在正常条件下不依赖人工干预完成全流程？

#### 结论：**在当前主干代码下：无法稳定自主完成**。

- **已确认事实**：
  1. **环境与特权盲区**：在普通受限环境或沙盒下，`stage_skills.py` 在启动阶段就会抛出 `PermissionError` 导致第一阶段都无法开始；
  2. **时序盲区**：即便在无特权问题的环境中，`plan` 节点并发启动的 Challenger 极易因时间戳倒挂（`chal_ts < plan_ts`）或缺失引用触发拒绝；
  3. **验收断链**：一旦第一或第二阶段交付物格式有任何瑕疵，F-01 会立刻导致工作流静默挂起。
  因此，在当前未修复的代码基准上，除非人工全程护航（手动修时间戳、手动修格式、手动 set completed），否则无法依靠纯自动化机制走完 6 个阶段。
- **证据**：
  - 本次审计现场运行 `pytest -v tests/test_software_development_v1_e2e_isolated.py::test_01_normal_six_stage_lifecycle_e2e`，第一阶段即抛出 `PermissionError` 失败。
- **未验证的部分**：在彻底隔离了受限目录后，使用真实外部模型（如 Claude API）连续执行 10 次真实项目全流程的端到端统计完成率。
- **建议行动与预期评估**：
  优先修复本次审计提出的 P0 级三项最小缺陷，预期能够消除当前已确认的断路死胡同与时序竞争；随后需在隔离的真实多 Worker 环境中重新进行端到端验证以评估实际自主完成率改善程度。

---

### Q6：如果只能做三项最小修复，哪三项最可能改善自动完成率？

#### 结论：最值得优先实施的三项最小修复（P0 组合拳）：

| 序号 | 修复项 | 涉及文件与修改点 | 预期效果与机制对齐 |
|---|---|---|---|
| **1** | **补全 `task_events.py` 自动返工闭环** | 修改 [`services/controller/task_events.py:48-64`](../../../services/controller/task_events.py#L48-L64)，当 `try_auto_accept()` 失败时，不直接 return，而是调用现有的 [`herdr/task_cli_rework.py:handle_rework()`](../../../herdr/task_cli_rework.py) 进行受控返工派发（传递具体拒绝原因，在预算内重试）。 | **消除“验收未过即无后续动作”的停滞**，复用既有 `handle_rework` 闭环，使任务获得原子状态跃迁与修正指令下发。 |
| **2** | **将 `plan` 节点的 Executor 与 Challenger 调整为因果先后派发** | 修改 [`herdr/direct_dispatch.py:898-933`](../../../herdr/direct_dispatch.py#L898-L933)，`plan` 节点就绪时先派发 `executor`；待其产出计划后自动将计划内容注入并派发 `challenger`。 | **消除因时序倒挂与缺少引用造成的计划校验拒绝**，使计划节点按因果依赖正常推进。 |
| **3** | **对受限环境系统调用实施严格异常防御 (保持 Fail-Closed)** | 在 [`herdr/stage_skills.py:100`](../../../herdr/stage_skills.py#L100) 增加 `(OSError, PermissionError)` 容错跳过无效路径；在 [`herdr/git_coordination.py:82`](../../../herdr/git_coordination.py#L82) 显式抛出结构化环境异常拒绝操作（保持 Fail-Closed）。 | **修复底层系统调用未受控抛出导致的调度中断**，同时确保 Git 并发互斥安全性不被静默绕过。 |

---

## 4. 交付总结

本次审计表明：**HAFlow 不需要、也不应该推倒重来搞“大一统决策引擎”**。

系统的核心算法资产（DAG 模型、Critical-Path Scheduler、Candidate Freeze、Fix-Loop 指纹、CAS 状态机）是非常扎实和领先的。目前阻碍全流程自主完成的“拦路虎”，恰恰是近期在剥离全局总指挥（PR #356）过程中遗留的**几个断路分支、时序倒挂和容错缺口**。

通过聚焦实施上述三项最小修复，先期打通已知的确定性断链与异常死锁，再配合隔离环境下的多 Worker 端到端实测验证，逐步推进全流程自主完成率的稳步改善。
