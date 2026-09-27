# Adaptive Router — Controlled Rollout v1

`#103 Canary → #104 Controlled Rollout`：Canary 证明推荐可以真实执行一小部分，
Rollout 只解决“已进入 Canary 的 bucket 如何安全、可审计、可回退地扩大流量”。
不解决评分、分流、Outcome 生成、指标计算 —— 全部复用既有能力。

```text
Promotion is manual. Rollback can be automatic. Safety always wins.
扩量人工决定，止损系统可自动，回退永远优先，事实复用 #103。
```

## 1. 单位与阶段

Bucket = `recommended_agent × node × task_type`，独立维护状态。
第一版只允许闭枚举 `off/5/10/25/50`（`off=0`），无 75/100、无任意整数。

推进相邻：`off→5→10→25→50`；禁止跨级（`5→50` 拒绝）；
紧急回退可直接到 `off`（`50→off` 立即生效）。

## 2. 状态模型

`Current State + Immutable Audit Events`，SQLite 同库两表：

- `rollout_state(bucket_key PK, agent, node, task_type, percentage, updated_at, updated_by, reason)` —— 当前值；
- `rollout_audit(id PK, bucket_key, agent, node, task_type, previous_percentage, new_percentage, action, reason, source, algorithm_version, created_at)` —— 每次变化恰好一条，只增不改。

`state_db.apply_rollout_stage_atomic` 用 `BEGIN IMMEDIATE` 把 upsert + audit
包进同一事务：崩溃与并发双写都不会留下无审计的状态或半写。

Audit 契约字段：`recommended_agent/node/task_type/previous_percentage/new_percentage/action(promote|rollback|auto_rollback)/reason/source/created_at/algorithm_version=adaptive-router-rollout-v1`。

## 3. 与 Canary 的集成

复用 `#103` 确定性分流，不重设 hash：

```text
hash_bucket = sha256("canary-v2|{run_id}|{task_id}") mod 100
divert ⟺ hash_bucket < effective_percentage
```

`5% ⊂ 10% ⊂ 25% ⊂ 50%` 单调包含：升级后旧身份仍在新集合内，不洗牌。
`No persisted canary decision, no canary execution` 继续成立：真分流仍在
`agent_router` 临界区内先落 `route_decision(mode=canary, diverted)` 再提交；
持久化失败回 Legacy。

`effective_percentage` 解析（`herdr/rollout_policy.py`）：

```text
kill switch false → 0
已迁移 bucket（有 staged 行）→ staged 值（含显式 off）
未迁移 bucket → canary 配置 fallback（保持 #103 行为）
非法身份/存储异常/未知阶段 → 0（永不扩大）
rollout 模块本身不可用 → 0（percentage_source=rollout_unavailable）
```

`herdr/canary_router.py` 只消费该值（`gate.effective_percentage`，附带
`config_percentage` / `percentage_source` / `guard_forced_legacy` 供审计），
评分公式、准入、池/健康/隔离过滤、显式指定 bypass 均不动。

## 3a. 幂等与并发

- 重复设置当前阶段是 `action="noop"`：不是状态变化，不写审计；
- `apply_rollout_stage_atomic` 在 `BEGIN IMMEDIATE` 内校验 expected-previous：
  并发写者中败者拿到明确冲突错误，**不会有“旧状态覆盖新状态”或无审计的变化**；
- Router 只读 staged 值，SQLite 单条读要么看到旧值要么看到新值，不存在中间态。

## 4. Safety Guard（只自动止损，不自动扩量）

`herdr/rollout_policy.py:evaluate_guard` 只读消费
`herdr/canary_evaluation.py` 两臂 facts（qualified success、blocked/human
均值、样本数），不重建聚合、不新增指标：

- 集中阈值：`MIN_SETTLED_SAMPLES=20`（总量）、`MIN_ARM_SAMPLES=8`（每臂）、
  `SUCCESS_DROP_TOLERANCE=0.20`、`BLOCKED_TOLERANCE=0.30`、`HUMAN_TOLERANCE=0.30`；
- 样本不足 → 安静（不定罪），可关闭（`HERDR_ROLLOUT_GUARD_ENABLED=0`）；
- 触发 → `maybe_auto_rollback` 持久化 `→off`（`action=auto_rollback`）；
- 读预算有界：`GUARD_DECISION_LIMIT=200` 匹配决策、`GUARD_SCAN_CAP=400` 扫描预算；
- 触发判定只覆盖本 bucket（node/task_type 过滤后按精确身份选桶，不跨 bucket 牵连）。

自动止损的两种运行方式：

| 方式 | 触发者 | 语义 |
| --- | --- | --- |
| `rollout check-guard --auto-rollback`（默认路径） | 运维 / 外部巡检 | 评估并持久化 `→off` 审计 |
| `HERDR_ROLLOUT_HOT_GUARD=1`（opt-in） | 每次 dispatch | 只读，触发（或评估异常）时本次路由走 Legacy，不写库 |

热路径 guard 默认关闭：它要在 router 临界区内做一次有界决策扫描，默认开启会给
每次派发增加开销；开启即接受“用派发延迟换立即止损”。无论哪种方式，Guard 异常、
评估不可用、DB 不可用一律更保守（本次 Legacy / 不扩大）。

## 5. Kill Switch

`HERDR_ADAPTIVE_ROLLOUT_ENABLED=false` → 所有 bucket `effective=0`，
立即停止 Adaptive diversion；历史 state/route_decision/Outcome 保留，
重开后可审计。未设置默认启用（空 rollout + 无 canary 配置仍为 0，
默认关闭）。

## 6. CLI

```bash
herdr-task rollout status [--json]
herdr-task rollout set --agent codex --node implementation --task-type fix \
  --percentage 10 --reason "reviewed canary results"
herdr-task rollout off --agent codex --node implementation --task-type fix \
  --reason "manual rollback"
herdr-task rollout history [--agent --node --task-type --limit N] [--json]
herdr-task rollout check-guard --agent codex --node implementation \
  --task-type fix [--auto-rollback]
```

`set/off` 要求非空 `--reason`；非法过渡/开放百分比 exit 2；并发冲突 exit 2
（状态未变）；存储失败 exit 1 且无部分生效。`status` / `history` /
`check-guard`（无 `--auto-rollback`）只读，纯路径解析，不创建/迁移数据库。

## 7. 文件边界

- `herdr/rollout_policy.py` —— 阶段/过渡/审计/ effective/guard（唯一状态管理器）；
- `herdr/state_db.py` —— 追加两表 DDL + 原子 helper（无业务判断）；
- `herdr/canary_router.py` —— 消费 effective + guard 只读（最小接入）；
- `herdr/agent_router.py` —— 无改动（经 canary plumbing 间接消费）；
- `bin/herdr-task` —— rollout 子命令；
- `tests/test_rollout_policy.py` —— 专项契约；
- 本页 + `docs/references/cli-reference.md` + `wiki/log.md`。
