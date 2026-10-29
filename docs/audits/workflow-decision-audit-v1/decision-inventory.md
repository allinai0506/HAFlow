# HAFlow 节点决策规则全量提取清单 (Decision Inventory)

> 审计对象：`software-development-v1` 及相关 Controller / Engine 决策代码  
> 审计基线：Git Commit `739ef0d`  
> 审计日期：2026-10-29

---

## 1. 核心决策规则清单

本清单系统性提取影响工作流节点生命周期流转的全部判断规则，覆盖依赖判定、调度准入、运行巡检、成果验收、门禁裁决、重试恢复与收尾终止。

### 规则表

| 规则 ID | 判断位置 (文件:行号:函数) | 输入条件 | 决策结果 | 后续动作 | 规则优先级 | 失败 / 异常行为 | 证据代码与测试 |
|---|---|---|---|---|---|---|---|
| **RULE-DAG-01** (节点依赖就绪) | [`herdr/workflow.py:606`](file:///Users/user/haflow/herdr/workflow.py#L606): `get_ready_nodes()` | 当前 `completed_node_ids` 集合；节点声明的 `depends_on` 列表 | 允许就绪 / 等待依赖 | 将符合依赖且未完成的节点加入 `ready` 列表 | 最高 (DAG 基础) | 依赖缺失或输入非法时返回空列表，不推进 | [`test_workflow_engine.py`](file:///Users/user/haflow/tests/test_workflow_engine.py) |
| **RULE-STAT-01** (节点完成态判定) | [`services/controller/stage_state.py:123`](file:///Users/user/haflow/services/controller/stage_state.py#L123): `is_node_complete()` ➔ [`herdr/scheduler.py:182`](file:///Users/user/haflow/herdr/scheduler.py#L182): `node_is_complete()` | 当前节点所有 Task 的 `status` 与 `integration_mode`；节点声明的 `required_task_ids` | 完成 (`True`) / 未完成 (`False`) | 控制器判定该节点是否进入 `completed_nodes` 集合 | 最高 (前置于 DAG 计算) | 活跃任务集合为空返回 `False`；Git 模式任务若未到达 `integrated` 则视为未完成 | [`herdr/scheduler.py:225-230`](file:///Users/user/haflow/herdr/scheduler.py#L225-L230) |
| **RULE-DISP-01** (调度持久租约占位) | [`services/controller/advancement.py:356-381`](file:///Users/user/haflow/services/controller/advancement.py#L356-L381): `check_workflow_stage_advance()` | SQLite `node_dispatch_operations` 状态；`claim()` 是否成功 | 允许调度 / 阻止并跳过 | 成功获取租约后清除旧 JSON 锁，允许进入排队 | 优先于内存排队 | 获取租约失败（并发冲突）则 `continue` 等待下轮 sweep | [`test_node_dispatch_contract.py`](file:///Users/user/haflow/tests/test_node_dispatch_contract.py) |
| **RULE-DISP-02** (阶段排队排他锁) | [`services/controller/stage_state.py:26`](file:///Users/user/haflow/services/controller/stage_state.py#L26): `mark_stage_advance_queued()` | `stage-state.json` 中 `workflow:node` 的当前值 | 允许入队 / 拒绝重复入队 | 写入 `"queued"`，将 `stage_advance` 事件放入 `coordinator_queue` | 内存排队前置 | 键已为 `"queued"` 或 `"notified"` 时返回 `False`，避免事件泛洪 | [`test_controller_progress_actions.py`](file:///Users/user/haflow/tests/test_controller_progress_actions.py) |
| **RULE-CAND-01** (实现后候选版本冻结) | [`services/controller/advancement.py:46-76`](file:///Users/user/haflow/services/controller/advancement.py#L46-L76): `_scheduler_resolve_candidate_and_plan()` | `is_node_complete("implementation")` 为 `True`；`project_root` Git HEAD | 冻结 SHA (`frozen_sha`) / 挂起 (`deferred`) | 记录 `candidate_frozen` 事件于 `state.db`，供下游 `test` 和 `review` 绑定 | 优先于下游 DAG 推进 | 无法证明 Git HEAD 时标记 `deferred=True`，跳过下游节点派发 | [`test_frozen_base_candidate_dispatch.py`](file:///Users/user/haflow/tests/test_frozen_base_candidate_dispatch.py) |
| **RULE-JOIN-01** (汇聚门禁一致性校验) | [`herdr/scheduler.py:650`](file:///Users/user/haflow/herdr/scheduler.py#L650): `evaluate_join_gate()` | 节点的 `depends_on` 前置分支 Task 列表；工作流当前冻结的 `candidate_sha` | `join_satisfied` / `join_waiting` / `join_blocked` / `join_candidate_mismatch` | 满足时放行下游派发；不满足时阻止推进 | 优先于汇聚节点派发 | 依赖任务 SHA 不一致或未通过时阻止，不写锁，下轮 sweep 重估 | [`test_downstream_dispatch_contract.py`](file:///Users/user/haflow/tests/test_downstream_dispatch_contract.py) |
| **RULE-PLAN-01** (计划成果与对抗闭环核验) | [`herdr/plan_parser.py:447`](file:///Users/user/haflow/herdr/plan_parser.py#L447): `verify_plan_and_challenger()` | `notes.jsonl` 或克隆中的 `plan.md` 和 `challenger-review.md`；时序戳 `ts`；`Plan-Ref` 引用 | 核验通过 / 明确拒绝 (`refusal`) | 提取任务清单进入后续解耦校验；失败时返回错误码 | 优先于实现节点派发 | 任一条件不满足（缺结构、缺通过、缺引用、时序倒挂）返回 `mode: "refusal"` | [`test_software_development_v1_adapter.py:350-480`](file:///Users/user/haflow/tests/test_software_development_v1_adapter.py) |
| **RULE-PLAN-02** (解耦任务并发合法性校验) | [`herdr/plan_parser.py:250`](file:///Users/user/haflow/herdr/plan_parser.py#L250): `validate_task_decoupling()` | 解析出的任务修改文件列表 `files`；任务间依赖 `dependencies`；`max_concurrency` (3) | 允许并发 / 拒绝派发 (`refusal`) | 生成 1~3 个实现 TaskSpecs 直接派发 | 紧跟 RULE-PLAN-01 | 文件重叠、存在内部依赖或超过预算时强制 `mode: "refusal"`，拒绝派发 | [`herdr/plan_parser.py:270-320`](file:///Users/user/haflow/herdr/plan_parser.py#L270-L320) |
| **RULE-ACPT-01** (非门禁成果规则化验收) | [`services/controller/auto_acceptance.py:42`](file:///Users/user/haflow/services/controller/auto_acceptance.py#L42): `try_auto_accept()` | `node` 标识；成果文本（正则提取）；`verify-baseline` (实现节点) | 验收通过 (`completed`) / 验收失败 (`False`) | CAS 更新 Task 状态为 `completed` 并触发 `finalize_completed_task` | 优先于总指挥回合 | 匹配失败返回 `False` | [`test_software_development_v1_adapter.py:180-260`](file:///Users/user/haflow/tests/test_software_development_v1_adapter.py) |
| **RULE-ACPT-02** (Staged 流程事件旁路中断) | [`services/controller/task_events.py:48-64`](file:///Users/user/haflow/services/controller/task_events.py#L48-L64): `_handle_task_event()` | `is_staged_dev_flow_workflow(_wf_cfg)` 为 `True`；`try_auto_accept()` 返回 `False` | 记录关注 (`attention`) 并截断退出 | 写入 `attention.json` (`auto_acceptance_rejected`)，直接 `return` | 截断总指挥回退 | 流程静默中止在 `agent_done`，不产生重试或唤醒 | 见第 4 节 Finding B |
| **RULE-GATE-01** (门禁节点规则化裁决) | [`services/controller/auto_verdict.py:95`](file:///Users/user/haflow/services/controller/auto_verdict.py#L95): `try_auto_verdict()` | `gate-verdicts/<task_id>.json` 文件或终端标记 `HERDR_GATE_VERDICT:` | 裁决成功 / 无法裁决 (`False`) | 调用 `herdr-task set <task_id> completed --verdict pass|blocked` | 门禁节点专属 | 信号缺失或矛盾时返回 `False`，在 staged 流程中同样触发 RULE-ACPT-02 挂起 | [`test_issue_250_controller_gate_verdict_recovery_receipt_v1.py`](file:///Users/user/haflow/tests/test_issue_250_controller_gate_verdict_recovery_receipt_v1.py) |
| **RULE-RETRY-01** (门禁阻断与返工触发) | [`services/controller/advancement.py:212`](file:///Users/user/haflow/services/controller/advancement.py#L212): `check_workflow_stage_advance()` ➔ [`services/controller/fix_loop.py:handle_fix_loop()`](file:///Users/user/haflow/services/controller/fix_loop.py) | 门禁节点结论为 `blocked`；`fix_loop_count < FIX_LOOP_MAX` (3) | 触发返工 / 耗尽升级人工 | 作废目标节点任务为 `superseded`，在 `stage-state.json` 写入 `pending_redo` 闩 | 推进核验前置 | 超过 3 次触发 `attention_note` (`fix_loop_exhausted`)，停止自动推进 | [`test_fix_loop_gates.py`](file:///Users/user/haflow/tests/test_fix_loop_gates.py) |
| **RULE-LATCH-01** (返工闩重做前阻断) | [`services/controller/fix_delivery.py:103`](file:///Users/user/haflow/services/controller/fix_delivery.py#L103): `_fix_loop_latch_blocks()` | `stage-state.json` 存在 `pending_redo`；目标谱系是否存在更新的成功重做任务 | 阻止下游推进 (`True`) / 放行并清除闩 (`False`) | 存在未完成重做时阻止下游就绪；完成时原子清除闩及计数 | 下游 DAG 派发前置 | 阻断期间每 2s 打印一次等待日志 | [`test_158_fix_loop_fingerprint.py`](file:///Users/user/haflow/tests/test_158_fix_loop_fingerprint.py) |
| **RULE-GIT-01** (代码终化提交与合流) | [`services/controller/git_finalize.py:11`](file:///Users/user/haflow/services/controller/git_finalize.py#L11): `finalize_completed_task()` | 任务 `integration_mode == "git"`；工作区状态受控 | 终化成功 / 重试 / 失败升级 | 依次执行 `herdr-task commit` (`status -> committed`) 和 `herdr-task integrate` (`status -> integrated`) | 任务完成时触发 | 遇冲突自动尝试 rebase；超过 `FINALIZE_RETRY_MAX` (5) 升级 `waiting_human` | [`test_workflow_finalize.py`](file:///Users/user/haflow/tests/test_workflow_finalize.py) |
| **RULE-TERM-01** (全流程关闭完成判定) | [`services/controller/advancement.py:190`](file:///Users/user/haflow/services/controller/advancement.py#L190): `check_workflow_stage_advance()` | `is_workflow_completed()` 为 `True`；门禁节点无未决 blocked 结论；未置 `suppress_auto_close` | 允许关闭工作流 | 打印 `[WORKFLOW COMPLETE]`，调用 `maybe_close_completed_workflow()` | 终端收尾 | 关闭失败时下次 sweep 重试，完成日志受闩保护避免刷屏 | [`test_task_cli_workflow_close.py`](file:///Users/user/haflow/tests/test_task_cli_workflow_close.py) |

---

## 2. 八大异常决策模式深度审查

基于以上提取的规则，深入对照审查发现如下 8 种高风险决策反模式：

### 1. 同一条件在多个模块重复判断
- **典型案例：`_scheduler_join_gate_allows()`**  
  在 [`advancement.py:323`](file:///Users/user/haflow/services/controller/advancement.py#L323)（阶段推进扫描）、[`stage_delivery.py:64`](file:///Users/user/haflow/services/controller/stage_delivery.py#L64)（队列派发准入）以及 [`direct_dispatch.py:81`](file:///Users/user/haflow/services/controller/direct_dispatch.py#L81)（直派执行前夕）**连续 3 次被重复计算**。
- **危害**：各处加载的 Task 快照时间点不一致，且若判定异常各处处理策略不同（`advancement.py` 默默 `continue`，`stage_delivery.py` 阻断，`direct_dispatch.py` 回退 fallback），产生非确定性行为。

### 2. 不同模块对同一状态有不同解释
- **典型案例：完成态判定差异**  
  - 调度器核心 [`herdr/scheduler.py:227`](file:///Users/user/haflow/herdr/scheduler.py#L227) 要求 Git 模式任务必须达到 `status in ("integrated", "cleanup_ready", "cleaned")` 才算完成；
  - 但状态机转换 [`herdr/transitions.py:53`](file:///Users/user/haflow/herdr/transitions.py#L53) 与某些外壳判断仅检查 `status in COMPLETED_TASK_STATUSES`（包含了 `completed` 和 `committed`）。
- **危害**：当任务刚执行完 `commit` 尚未 `integrate` 时，外壳可能认为任务已完结，但调度器判定节点未完成，造成阶段推进与任务状态短暂脱节。

### 3. Agent 负责决定本应由编排引擎决定的流程流转
- **典型案例：自然语言文本中的 `HERDR_GATE_VERDICT:`**  
  编排引擎将测试与评审是否通过的硬性状态流转，完全寄托在 Agent 是否在终端或 Markdown 文件中打印了格式分毫不差的 `HERDR_GATE_VERDICT: pass` 字符串。
- **危害**：若模型输出带有 Markdown 强调符（如 `**HERDR_GATE_VERDICT:** pass`）或拼写略有变动，状态机无法识别，导致裁决直接失败。

### 4. Controller 依赖 Agent 的自由文本输出进行流程控制
- **典型案例：`validate_requirements_deliverable_content()`**  
  在 [`herdr/stage_skills.py:428`](file:///Users/user/haflow/herdr/stage_skills.py#L428) 中，Controller 使用硬编码中文正则（如 `(用户目标|需求目标|...)`、`(功能范围|需求范围|...)`）来裁决需求阶段是否合规。
- **危害**：若 Agent 使用英文产出高水平技术规格（如 `## Objectives and Goals`），全部正则匹配落空，返回缺少 5 大段落，进而直接导致自动验收拒绝。

### 5. 某些状态没有明确的后续动作（死胡同状态）
- **典型案例：PR #356 Staged 流程事件旁路中断**  
  在 [`services/controller/task_events.py:53-64`](file:///Users/user/haflow/services/controller/task_events.py#L53-L64) 中：
  当一个任务在 `software-development-v1` 中触发 `event_type == "done"`，但 `try_auto_accept()` 因上述正则不符等原因返回 `False` 时，代码直接 `return` 并记录 Attention Note。
- **危害**：该 Task 既没有进入 `completed`，也没有进入 `rework` 或 `failed`，而是死锁在 `agent_done`。Controller 没有安排任何重试或自动修复逻辑，工作流就此彻底停滞。

### 6. 多个恢复机制重复处理同一个故障
- **典型案例：看门狗超时与基础设施恢复竞态**  
  - [`services/herdr-sentinel.py`](file:///Users/user/haflow/services/herdr-sentinel.py) 的 `hard_timeout_watchdog` 会将超时的任务强制写为 `failed`；
  - 随后 Controller [`services/controller/registry.py:153`](file:///Users/user/haflow/services/controller/registry.py#L153) 的 `recover_infra_failed_tasks()` 尝试自动作废补派；
  - 与此同时，[`services/controller/auto_verdict.py:83`](file:///Users/user/haflow/services/controller/auto_verdict.py#L83) 又试图从该 `failed` 任务所属克隆中抢救 `gate-verdict.json` 实施自动翻案。
- **危害**：两个异步恢复过程竞争修改同一任务的生命周期状态，导致 CAS 乐观锁冲突或状态震荡。

### 7. 交付物已经合格，但节点无法推进
- **典型案例：Challenger 缺少 `Plan-Ref` 字段**  
  在 [`herdr/plan_parser.py:550-555`](file:///Users/user/haflow/herdr/plan_parser.py#L550-L555) 中：即使 Challenger 的质询内容非常充实，并给出了明确的通过裁决，但如果它在自由 Markdown 中没有显式写出 `Plan-Ref: <note_id>`，系统直接判为 `challenger_plan_ref_missing`，`plan_stage_dispatch` 报告 `refusal`，导致 `implementation` 节点永远无法派发。

### 8. 节点已经失败，但仍被重复调度
- **典型案例：`waiting_human` 状态穿透**  
  在 [`services/controller/stage_state.py:168`](file:///Users/user/haflow/services/controller/stage_state.py#L168) 修复前，节点持久化调度操作已进入 `waiting_human`（达到重试上限待人工裁决），但阶段状态锁被错误清除，Controller 每轮 sweep 继续触发相同派发，重复报错刷屏。
