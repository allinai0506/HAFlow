# Adaptive Agent Router v1 (Shadow Mode)

## 1. 为什么需要 Adaptive Router

现有 `choose_agent` 按静态偏好 + 实时负载选择 Agent，从不回头看历史：
HAFlow 已积累 trajectory / metrics / eval / verification 事实，却没有反过来
影响下一次调度。本模块第一次用真实运行历史衡量每个 Agent 在
(agent × node/stage × task_type) 桶中的实际表现，只做推荐，不做决策。

## 2. Qualified Success 是什么

一次 Run 只有同时满足以下条件才算 Qualified Success（缺一即未知或失败）：

```text
requirements_satisfied == true
AND verification_passed == true
AND final_status ∈ {completed, committed, integrated, cleanup_ready, cleaned}
```

事实来源是 `eval_results` 最新 revision（`herdr/eval_store.py`）。
没有完整 eval 行的 Run 是 outcome-unknown：不计入分母，绝不默认成功，
也绝不明示 `Task done == qualified success`。

## 3. ETQS 是什么

Expected Time To Qualified Success（秒，估算值）：

```text
ETQS = queue_delay_est
     + p50_wall_time (observed；无观测时用 FALLBACK_EXECUTION_SECONDS=600 估算)
     + (1 - blended_success_rate) × RECOVERY_PENALTY_SECONDS (600)
     + rework_rate × REWORK_PENALTY_SECONDS (300)
```

`verification_failure_rate`、`blocked_rate`、`human_intervention_rate`
只作为 observed 事实展示，不进入公式：blocked / 校验失败的 Run 本来就
拉低了 qualified_success_rate，再加一次属于重复惩罚同一 outcome。

`queue_delay_est = (active_load + reserved_load) × 30s`，来源恒为 estimated。

## 4. Shadow Mode 为什么不会影响生产

```text
choose_agent → _choose_agent_impl → actual_agent (唯一执行依据)
                                    └→ shadow (route_decision 事件，仅记录)
```

- `choose_agent` 的返回语句与排序逻辑逐行保持原语义；shadow 在决策
  完成后才运行，返回值恒为 impl 的选择。
- shadow 全程 try/except：任何异常只追加 `route_decision_error` 事件
  （再失败则静默），绝不阻断派发（fail-open to legacy）。
- 测试 `test_case9/10` 把这两条锁死为硬门禁。

## 5. 数据来源

全部复用既有事实源，不建新 source of truth：

| 指标 | 来源 |
| --- | --- |
| agent/node/stage/status/wall | `tasks` 表（`state_db.query_adaptive_history`）；`task_type` 由 launch 持久化（`bin/herdr-task`），缺失的 legacy 行归入 `""` 桶 |
| requirements/verification/human/final | `eval_results` 在 cutoff 之前的最新 revision，按 run_id + task_id 双归属；taskless eval（task_id IS NULL）仅当 run_id 可证明唯一属于一个 Task 时归属，否则 unknown；`final_status` 只取 eval 行，绝不用 task.status 回填 |
| rework / blocked | task `status_history` 中的状态机枚举值（非字符串猜测） |
| 持久化 | `StateStore.record_event("route_decision")`，source=`adaptive-router-shadow` |

查询按当前候选 agents 分片：每个 candidate 独立 newest-first 窗口
（默认 500 条），高频 Agent 挤不掉低频候选的历史。分片由
`idx_tasks_agent_node_created(agent, node, created_at)` 索引服务
（EXPLAIN 回归锁定无全表扫描），处在 dispatch 热路径上的同步查询
成本只与该 Agent 自身历史成正比。

`task_type` 缺失的 legacy task 归入 `""` 桶独立统计，不猜测。
`agent` 只取 `tasks.agent`（路由标签），不与 agent_name/type 混淆。

## 6. Ranking 公式

按 `ETQS` 升序；ETQS 相同按传入 candidate order（即旧 Router 偏好序）
tie-break，保证与旧行为兼容。输出每候选的 score breakdown
（sample_count、成功率、p50/p90、rework/blocked/verification/human、
queue、confidence、fallback_reason），无 opaque score。

## 7. 冷启动

`blended = (n×observed + K×prior)/(n + K)`，`K=10`，`prior=0.5`；
`confidence = n/(n+K)`。1 次 100%（blended≈0.55）打不过 50 次 92%
（blended≈0.85）。零有效样本时回退 candidate order 并标注
`fallback_reason=no_history`（有行但无确定 outcome 时为
`unknown_outcome_no_determined_samples`）。

## 8. Known limitations

1. 每 candidate newest-first 窗口默认 500 条；更老样本自然遗忘
   （lookback_days 可选开启时间下限）。
2. wall time 取 task 时间戳差，是执行时长近似，不是 Pane 真实存活测量。
3. queue delay 是负载线性估算，不是观测到的排队事实。
4. task_type 未持久化的老数据只能按 `""` 桶统计，跨桶不可比。
   2026-09-25 起 launch 持久化 task_type，此后新数据逐桶可用。
5. `herdr-task route-shadow` 未指定 `--agents` 时，候选集按项目池偏好顺序
   近似推导，未复刻 health/disabled/isolation 硬约束过滤；其输出是
   “历史表现排名”，不是“生产可派发集合”。
6. 本版只 Observe→Measure→Recommend；v2 是否接管流量需 shadow 数据证明
   ETQS 真实下降后另行决策。
