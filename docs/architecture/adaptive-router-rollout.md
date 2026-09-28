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

- `rollout_state(agent, node, task_type, percentage, updated_at, updated_by, reason)` —— 当前值，**复合主键 `(agent, node, task_type)`**；
- `rollout_audit(id PK, bucket_key, agent, node, task_type, previous_percentage, new_percentage, action, reason, source, algorithm_version, created_at)` —— 每次变化恰好一条，只增不改。

> ### 为什么不用分隔字符串做主键
>
> `"/"` 拼接的 key 无法区分 `("a/b","c","d")` 与 `("a","b/c","d")` —— 两者都得到
> `a/b/c/d`，于是两个不同 bucket 可能读写同一行状态。存储因此直接用**列级复合主键**。
> `rollout_bucket_key()` 只用于审计与 CLI 展示，改用 JSON 数组编码
> （`["a/b","c","d"]`），保证展示同样无歧义。早期按 `bucket_key` 主键建的表会被
> `_migrate_rollout_state_key` 按真实列原地迁移。

`state_db.transact_rollout_stage` 把**读-判-写**收敛进一个事务：

```text
BEGIN IMMEDIATE
  ↓ 读取真实快照 (exists, percentage)
  ↓ 校验 == 决策所依据的快照
  ↓ upsert state + append audit
COMMIT
```

Audit 契约字段：`recommended_agent/node/task_type/previous_percentage/new_percentage/action(promote|rollback|auto_rollback|takeover)/reason/source/created_at/algorithm_version=adaptive-router-rollout-v1`。

`action` 描述**真实流量方向**：`promote` ⟺ `new_percentage > previous_percentage`。
唯一例外是 `takeover`：**流量不变、所有权迁移** —— 无 staged 行、canary 配置正在
服务 N%、`set N` 时，写入显式 N 行并审计 `N → N`。这不是 no-op：no-op 什么都不
改变，takeover 把 bucket 从「配置兜底」迁移到「staged 行持有」，此后阶梯校验才有
真实的 staged 基准（`5→10` 才可能；否则系统只会看到 `0→10` 而拒绝）。
`50 → 5` 仍记 `rollback`（不是 `promote`）—— 首次接管**旧流量比例更高**时流量
真实下降。过渡合法性仍按 staged 阶梯校验，只有 action 的判定基准是有效比例；
takeover 绕过阶梯是因为流量没有变化，阶梯管的是流量变化。

## 2a. 并发：快照即身份

`RolloutSnapshot(exists, percentage)` 是 bucket 的完整状态身份，**`exists` 是身份
的一部分，不是细节**：

| 状态 | 含义 | 正在分流的百分比 |
| --- | --- | --- |
| `exists=False, 0` | 尚未接管 | canary 配置 fallback |
| `exists=True, 0` | 显式 off | 0 |

只比 `percentage` 的 CAS 分不清这两者，于是「读到 absent 的 promotion」可以静默
覆盖「刚写入 explicit 0 的紧急回退」。因此：

- **CAS 覆盖存在性**：事务内比对 `exists` + `percentage`；不匹配 → 冲突错误，
  紧急回退存活；
- **no-op 也要 CAS**：`write=None` 同样走事务校验，绝不在事务外判定“无需修改”
  再直接返回 —— 否则「读到 0 的 off」会在并发 promotion 之后仍报告 “already off”。

判定的纯函数是 `rollout_policy.decide_rollout_change(snapshot, ...)` → `RolloutDecision`；
持久化只负责事务与快照校验，业务判断不落在 `state_db`。

未来若要更强的并发控制，可在 `rollout_state` 增加 `revision` 列并纳入 CAS 身份
（当前 `BEGIN IMMEDIATE` + 全量快照比对已足够）。

**腐坏 stage 只能回到 off**：若磁盘上的 stage 落在闭枚举之外（例如 75），
`effective_percentage` 已按 fail-safe 解析为 0（不分流）。此时 `rollout off`
会**写入**显式 0 行来修复，而不是报 no-op、也不是因改写快照而必然 CAS 冲突 ——
否则运维会被困在无法回到已知状态的死路。腐坏期间任何其他 stage 一律拒绝
（`corrupt rollout state; only rollback to off is allowed`），腐坏不构成自动
扩量的许可。CAS 始终使用**原始**磁盘快照。

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

**一次决策来自一次快照**：`exists + percentage` 由 `state_db.read_rollout_snapshot`
在**一条查询**里读出。拆成两次读（先读值、再读存在性）会让「已 COMMIT 的紧急回退」
与「回退前的旧值」被拼成一次决策 —— 状态、证据、流量必须描述同一时刻。

**先校验、后转换**：`normalize_percentage` 与 config fallback 解析对任何非精确整数
输入（`5.9`、`NaN`、`inf`、`True`、`Decimal("5.9")`）一律拒绝/归零，绝不用
`int()` 截断 —— `int(5.9)` 曾经静默变成合法 stage 5。合法输入：闭枚举整数、
其字符串、`"off"`、整数值浮点/Decimal（`5.0`）。

> ### Absent ≠ explicit off
>
> 没有 staged row 的 bucket 仍在按 canary 配置分流。因此 `rollout off` 对这类
> bucket **必须写入 `percentage=0` 的显式行**来压制 fallback；把它当成 no-op 会
> 让 CLI 报告“已关闭”，而生产仍在跑 Adaptive 流量。同理，Safety Guard 触发时
> 不能因为“staged 读到 0”就报告 already off。
>
> `set_stage(..., config_fallback=N)` 用 canary 配置的真实百分比（由
> `canary_router.config_percentage_for` 提供，`None` = 该 bucket 无 canary 流量）
> 判断 no-op 与审计事实：
>
> | 场景 | 结果 |
> | --- | --- |
> | 无行 + config 50 + `off` | 写显式 0 行，审计 `50 → 0` |
> | 无行 + 无 config 流量 + `off` | 真 no-op，不写行 |
> | 有行 0 + `off` | 真 no-op |
> | 无行 + config 5 + `set 5` | 写 5，审计 `5 → 5` action=takeover（流量不变，所有权迁移） |
> | 无行 + config 50 + `set 5` | 写 5，审计 `50 → 5` action=rollback（记录真实分流变化） |

> 审计里的 `previous_percentage` 是**当时真正在分流的比例**（仅首次迁移时与
> staged 值不同），`expected_staged_percentage` 才是并发校验用的 staged 值，
> `action` 也按前者（真实分流比例）判定。

`herdr/canary_router.py` 只消费该值（`gate.effective_percentage`，附带
`config_percentage` / `percentage_source` / `guard_forced_legacy` 供审计），
评分公式、准入、池/健康/隔离过滤、显式指定 bypass 均不动。

## 3a. 幂等

- 重复设置当前阶段是 `action="noop"`：不是状态变化，不写审计；
- **同流量不总是 no-op**：无 staged 行 + config fallback N + `set N` 是
  `action="takeover"`（写行、写审计，所有权迁移）；只有 `exists=True` 且值相同
  （或无行、无流量、`set 0`）才是真 no-op；
- no-op 仍经过 §2a 的快照校验，不会从过期读取得出「已经 off」；
- Router 只读 staged 值，SQLite 单条读要么看到旧值要么看到新值，不存在中间态。

## 4. Safety Guard（只自动止损，不自动扩量）

`herdr/rollout_policy.py:evaluate_guard` 只读消费
`herdr/canary_evaluation.py` 两臂 facts（qualified success、blocked/human
均值、样本数），不重建聚合、不新增指标：

- 集中阈值：`MIN_SETTLED_SAMPLES=20`（总量）、`MIN_ARM_SAMPLES=8`（每臂）、
  `SUCCESS_DROP_TOLERANCE=0.20`、`BLOCKED_TOLERANCE=0.30`、`HUMAN_TOLERANCE=0.30`；
- 样本不足 → 安静（不定罪），可关闭（`HERDR_ROLLOUT_GUARD_ENABLED=0`）；
- 触发 → `maybe_auto_rollback` 持久化 `→off`（`action=auto_rollback`）；
- **证据只取当前 episode**：每次成功的显式阶段变更（promote/rollback/
  auto_rollback/takeover）开启新证据窗口，窗口起点 = 本 bucket 最新一条
  `rollout_audit.created_at`（`current_rollout_episode_start`，作为 `since`
  传给 canary_evaluation，判定结果经 `evidence_since` 暴露）。回退后重试 5%
  不会被上一轮坏样本立即再次定罪；5% 的好证据也不会证明 10% 安全 —— 每次
  变更后重新累积。无审计历史的 bucket（纯 config 兜底）保持 #103 的不加窗
  读取；历史数据永不删除，CLI history 仍展示全部；
- 读预算有界：`GUARD_DECISION_LIMIT=200` 匹配决策、`GUARD_SCAN_CAP=400` 扫描预算；
- 触发判定只覆盖本 bucket，并把整段 bucket 范围**下推到 SQL**
（`state_db.ExactDecisionBucket`：mode + recommended_agent + node + task_type）。
只按 node/task_type 过滤是不够的：同一 `implementation/fix` 下可能有 codex/claude/
opencode 多个 bucket，若更活跃的兄弟 bucket 吃掉全部 200 条决策预算，目标 bucket
就会读成「无样本」→ guard 安静 → 目标 bucket 明明在恶化却继续放行 Adaptive。
`agent` 过滤器语义太宽（同时匹配 actual/recommended/legacy），因此单独提供
`recommended_agent`。

下推到 SQL 是必需的，因为扫描预算 `scanned` 统计的是**每页返回的原始行数**：
只在 Python 侧过滤能保护 matched 预算，却保护不了 `scan_cap` —— 无关 bucket 与
shadow 决策照样会吃光它。该下推是**按 `recommended_agent` opt-in** 的：shadow 评估
需要靠 Python 侧 mode 过滤来统计 `skipped_canary_events`，因此从不传
`recommended_agent`，计数语义保持不变。

精确 bucket 读取由部分表达式索引 `idx_events_route_decision_bucket` 服务：
四个 bucket 表达式（各自包在 `CASE WHEN json_valid(payload_json) THEN … END`
里）+ `timestamp` + `id`，`WHERE event_type='route_decision'`。稀疏 bucket 不再
全扫 route_decision 历史；非法 payload 在索引中落为 NULL，INSERT 时的索引维护
永不失败（与 working-context 触发器同款 json_valid 防护）。索引只在可写 schema
初始化（`_ensure_schema`）中创建，只读守卫查询绝不建表建索引（#102 契约）。
查询的 WHERE 子句与索引定义共用同一表达式生成器
（`_route_decision_bucket_exprs`），保证 SQLite 稳定命中索引 —— 由
EXPLAIN QUERY PLAN 回归测试锁定（多兄弟行 + 少目标行，断言 SEARCH 而非 SCAN）。

判定结果用 `status` 区分「评过了」与「评不了」，因为两者要求的路由方向相反：

| status | triggered | 热路径路由 |
| --- | --- | --- |
| `triggered` | True | Legacy |
| `within_tolerance` | False | Adaptive |
| `insufficient_samples` | False | Adaptive（样本不足不是异常） |
| `unavailable` | False | **Legacy**（评不了 ≠ 没问题） |
| `disabled` | False | Adaptive（运维显式关闭） |

自动止损的两种运行方式：

| 方式 | 触发者 | 语义 |
| --- | --- | --- |
| `rollout check-guard --auto-rollback`（默认路径） | 运维 / 外部巡检 | 评估并持久化 `→off` 审计 |
| `HERDR_ROLLOUT_HOT_GUARD=1`（opt-in） | 每次 dispatch | 只读，触发（或评估异常）时本次路由走 Legacy，不写库 |

热路径 guard 默认关闭：它要在 router 临界区内做一次有界决策扫描，默认开启会给
每次派发增加开销；开启即接受“用派发延迟换立即止损”。开启后，**评估不可用
（DB 不可用、schema 错误、未知异常）一律走 Legacy**，绝不把“评不了”当作
“没问题”；关闭 guard（`HERDR_ROLLOUT_GUARD_ENABLED=0`）则由运维显式接管。

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
`check-guard`（无 `--auto-rollback`）走 #102 的只读连接（`mode=ro` +
`query_only`）：不建库、不建表、不迁移；pre-rollout 库读作“无 staged bucket”，
而读取失败（locked/corrupt/无权限）**不会**被压成“全部 off”，而是 exit 1 并
明确报 “unavailable”。

`set` 输出的 `action` ∈ `promote/rollback/auto_rollback/takeover/noop`。
`5% -> 5% action=takeover` 表示流量未变、所有权从 canary 配置迁移到 staged 行 ——
这是「config 兜底服务中」的 bucket 进入阶梯的入口动作；takeover 之后
`set 10` 才是合法的 `5 -> 10` promote。百分比参数在域边界先校验后转换：
`--percentage 5.9` 之类非精确整数输入直接 exit 2，绝不截断。

**唯一 DB 解析器**：读写共用 `state_db.resolve_state_db_path()`，写路径不再经
`_get_store()` 二次猜测 —— 否则在 `WORKFLOW_FILE` / `CHECKPOINTS_DIR` 等非默认
布局下可能出现「set 写库 A、status 读库 B」的 split-brain。

`check-guard` 在 `status=unavailable` 时**退出 1**（JSON 走 stderr）：监控系统
绝不能把「无法判定」读成「检查通过」。样本不足（`insufficient_samples`）仍是
正常判断，退出 0。

## 7. 文件边界

- `herdr/rollout_policy.py` —— 阶段/过渡判定（纯函数）/effective/guard（唯一状态管理器）；
- `herdr/state_db.py` —— 追加两表 DDL + 快照/事务（无业务判断）；
- `herdr/canary_router.py` —— 消费 effective + guard 只读（最小接入）；
- `herdr/agent_router.py` —— 无改动（经 canary plumbing 间接消费）；
- `bin/herdr-task` —— rollout 子命令；
- `tests/test_rollout_policy.py` —— 专项契约；
- 本页 + `docs/references/cli-reference.md` + `wiki/log.md`。
