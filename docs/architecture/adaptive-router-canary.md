# Adaptive Router v2 — Canary Mode

让 Adaptive Router 第一次真正参与少量生产决策：不是再评估一个预测，而是让
推荐 Agent 在严格保护下真实执行一小部分派发，然后用 Execution Outcome 证明
（或证伪）预测。这是 #99 Shadow → #100 Outcome → #101/#102 Evaluation 闭环
的最后一块：

```text
预测 → 少量真实执行 → Outcome → Canary Evaluation → 判断是否扩大流量(#104)
```

## 1. 流量模型

```text
Legacy Router (_choose_agent_impl)
      │
      ├── 不满足 Canary 条件 ──→ Legacy Agent（行为与 v1 shadow 时代逐字节一致）
      │
      └── 满足 Canary 条件
              ↓
      deterministic canary gate（sha256 mod 100 < percentage）
              ↓
        分流任务交给 Adaptive Router 推荐的 Agent
              ↓
      Execution Outcome（既有不可变事实层，无新写路径）
              ↓
      Canary Evaluation（herdr/canary_evaluation.py，只读）
```

## 2. 四个保护

1. **默认关闭**。配置文件缺失、`enabled` 非 true、或任何 schema 非法 → Canary
   完全不激活；路由行为与 Canary 代码不存在时一致。配置解析 fail-closed：
   非法值返回 `(None, errors)`，永不部分启用。
2. **只对白名单 bucket 生效**。bucket = `recommended_agent × node ×
   task_type` 精确匹配配置条目；不存在全局开关。分流目标必须来自当前路由
   已通过池约束（allowed/disabled/unhealthy/healthy 快照/stage 隔离）过滤的
   候选列表 —— 结构上不可能分流到 Legacy Router 无权选择的 Agent。
3. **确定性分流**。`sha256("canary-v2|{run_id}|{task_id}")` 前 8 字节整数
   mod 100 < percentage。不用 Python `hash()`（随机化）；同一身份在任何
   进程、任何时间落在同一 arm，run 可复现、可审计。`task_id` 与 `run_id`
   缺一即不分流（无法复现的身份不进 Canary）。改变 salt 或 percentage 会
   重排身份归属 —— salt 是持久化契约的一部分。
4. **一键回退 / fail-open**。Canary 规划任何异常（含 DB 读失败）→ 记
   `route_decision_error`（mode=canary，best-effort）→ 返回 Legacy Agent。
   配置损坏（含非法 UTF-8）或配置加载的任何意外失败 → 一律视为关闭。
   运维回退 = 把 `enabled` 改为 false 或删除配置文件，立即生效（每次路由
   重新读配置，无缓存）。

## 2a. 持久化门（No persisted canary decision, no canary execution）

真正的分流（hash 命中且推荐 != Legacy 选择）必须先落盘再提交：`route_decision`
(mode=canary, diverted=true) 在路由临界区**内部**、写 reservation **之前**
持久化（`record_event` 抛异常或返回 False receipt 即视为失败）。持久化失败 →
明确回到 Legacy 选择，reservation 记 Legacy Agent —— 永远不会出现"Adaptive
Agent 真实执行但没有 canary 决策事实"的孤儿分流。非分流决策（hash 未命中 /
两路由器同判 / 分流被回退）在锁外 best-effort 记录：执行本身走 Legacy 路径，
事件丢失只会让评估的 legacy 臂少一个样本，不会产生无法归属的执行。

## 3. 准入（sufficient-only）

复用 Shadow Evaluation 的权威判定（`herdr/shadow_sufficiency.py` +
`herdr/shadow_rows.py` 连接），不建平行事实源：

- `model_data_status = sufficient`：路由器预测时确有历史（冻结 sample_count ≥ 30）；
- `evaluation_data_status = sufficient`：冻结预测已与 settled Outcome 校准 ≥ 30；
- 两者同时 sufficient 的 bucket 才可能分流；
- 准入扫描有界（`admission_scan_cap`，默认 500）：窗口截断且无法证明
  sufficient 时拒绝准入 —— 准入不确定性一律 fail-closed。

准入扫描在 router 锁内执行（有界只读），因为它必须基于同一次锁内产生的
已验证候选列表，且 reservation 必须在同一临界区记录最终派发 Agent。代价是
启用 Canary 的 bucket 派发持锁时间增加；Canary 默认关闭且按 bucket opt-in。

## 4. 决策事件契约

每次路由恰好一条 `route_decision` 事件：

- Canary 准入通过 → `mode: "canary"`，`source: "adaptive-router-canary"`，
  `algorithm_version: "adaptive-router-v2-canary"`；payload 额外携带
  `legacy_agent`（Legacy 会选谁）、`diverted`（最终执行者是否偏离 Legacy）、
  `canary_gate`（bucket_key / hash_bucket / effective_percentage /
  hash_divert / would_divert / admission 状态与样本数 / truncated）。
  真分流（diverted=true）的决策在路由临界区内先持久化（见 §2a）。
- 未过门 → `mode: "shadow"`（与 v1 相同）。
- `actual_agent` 恒等于真正执行的 Agent：Outcome 归属契约
  （`decision.actual_agent == outcome.agent`）对分流与非分流执行同等成立。

## 5. 与 Shadow Evaluation 的边界

Shadow 的语义是"recommended 从未执行，uplift 只是反事实预测"。Canary-diverted
执行破坏了这个前提（recommended 真的跑了），若混入会把 agreement 虚高。
因此 shadow 集合跳过 `mode="canary"` 事件并在 collection meta 计数
（`skipped_canary_events`）；canary 事件由 Canary Evaluation 独占。

Canary Evaluation 的臂样本同样遵循 #101 的 execution 去重
（`select_authoritative_execution_rows`）：一个 `(task_id, run_id)` 无论
产生多少次决策，至多贡献一个臂样本；bucket 按 `recommended_agent × node ×
task_type` 划分，与准入单位一致。

共享的 bounded-scan 实现在 `shadow_rows._collect_rows_with_meta`，以
`mode` 参数选择切片：`"shadow"`（默认，排除 canary）、`"canary"`（仅
canary）、`"all"`（全部，Canary 准入证据）。

## 6. Canary Evaluation（观测事实，不是 rollout 结论）

`herdr/canary_evaluation.py` / `herdr-task canary-eval`（只读）：

- **adaptive arm**：`diverted=true` 的 canary 决策（推荐 Agent 真实执行）；
- **legacy arm**：`diverted=false` 的 canary 决策（hash 未命中或两路由器
  同判 —— 同 bucket、同时间窗的确定性对照组，这是 Shadow 阶段拿不到的）；
- 每臂指标：qualified success（= 最终 qualified completion：requirements ∧
  verification ∧ completed）、median wall time、mean rework / blocked /
  human intervention、ETQS 近似 MAE（冻结预测 vs 同次 wall time）；
- delta = adaptive − legacy，只报事实；样本小，不声称显著性；
  **是否扩大流量是 #104（Controlled Rollout），始终是人工决策**。

## 7. 运维

- 配置：`~/.herdr-controller/route-canary.json`（或 `HERDR_ROUTE_CANARY_CONFIG`），
  schema 与字段见 `docs/references/cli-reference.md` 第 6 节；
- 评估：`herdr-task canary-eval [--json]`；影子对照：`herdr-task shadow-eval`；
- 回退：`enabled: false`（或删文件）→ 立即全量 Legacy；
- 预期日志：分流与不分流都只留 `route_decision` 一条；异常留
  `route_decision_error`（mode=canary）。
