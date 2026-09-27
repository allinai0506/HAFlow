# 异构 Agent 路由策略与并发锁 (agent-routing-and-pools.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> **多 Agent 调度、优先级评分、负载均衡与 Reservation 预占锁**  
> 关联索引: [[index]] | [[domain-model]] | [[preflight-and-health]] | [[task-lifecycle]]

---

## 1. 路由优先级与选人决策树

在任务派发（`choose_agent`）时，系统按以下严格优先级梯次裁决执行 Agent：

```mermaid
flowchart TD
    Start([选择执行 Agent]) --> CheckWF{存在 Workflow 级覆盖?<br/>agent_override != 'auto'}
    CheckWF -- 是 --> PickWF[采纳 Workflow 覆盖值]
    CheckWF -- 否 --> CheckReq{用户显式指定 Agent?<br/>requested != 'auto'}
    CheckReq -- 是 --> PickReq[采纳显式指定值]
    CheckReq -- 否 --> CheckFixed{Node Policy 存在 fixed?}
    CheckFixed -- 是 --> PickFixed[采纳固定配置 fixed]
    CheckFixed -- 否 --> GenCandidates[动态生成候选人有序列表]

    GenCandidates --> Pref[1. Node Policy: preferred]
    Pref --> StagePref[2. Project Pool: stage_preferences]
    StagePref --> TaskPref[3. Project Pool: task_type_preferences]
    TaskPref --> Allowed[4. Project Pool: allowed_agents]

    Allowed --> FilterEx[过滤排除: node_policy.exclude]
    FilterEx --> FilterDis[过滤禁用: pool.disabled_agents]
    FilterDis --> FilterUnhealthy[过滤不健康: unhealthy_agents 永不入选]
    FilterUnhealthy --> FilterHealth{快照新鲜?<br/>preflight_checked_at 在 TTL 内}
    FilterHealth -- 是 --> FilterHealthy[正向交集: healthy_agents]
    FilterHealth -- 否 --> SkipHealthy[过期快照降级: 仅做减法]

    FilterHealthy --> CheckEmpty{候选集为空?}
    SkipHealthy --> CheckEmpty
    CheckEmpty -- 是 --> Error[抛出 RuntimeError: No enabled Agent available]
    CheckEmpty -- 否 --> LoadBalancing[执行多维负载均衡计算]
```

Evidence:
- `herdr/agent_router.py#_candidate_order`
- `herdr/agent_router.py#choose_agent`
- `herdr/agent_router.py#preflight_snapshot_fresh`
- `tests/test_agent_router_preflight.py`

---

## 2. 负载均衡与最少活跃优先 (Least Loaded)

当产生多个合规候选 Agent 时，系统通过最小负载评分进行排序：
- `FACT` **活跃任务负载 (`active_loads`)**: 统计 `tasks.json` 中属于该项目且状态为未终结（`pending` 到 `cleanup_ready` 之间）的各 Agent 任务总数。
- `FACT` **锁预占负载 (`reserved_loads`)**: 统计 `agent-reservations.json` 中当前被预占但尚未落盘到 `tasks.json` 的各 Agent 数量。
- `FACT` **综合评分**:
  $$\text{Score}(Agent) = \text{ActiveTasks}(Agent) + \text{ReservedTasks}(Agent)$$
  具有最低综合得分的候选 Agent 将被优先选中；当得分相同时，维持候选顺序中靠前的 Agent。

Evidence:
- `herdr/agent_router.py#_active_agent_loads`
- `herdr/agent_router.py#choose_agent` (排序键: `active_loads + reserved_loads, item[0]`)

---

## 3. 并发死锁防御：Reservation 预占锁与 TTL

### 3.1 预占锁解决的核心冲突
在并发派发多个并行任务时，如果从“选定 Agent”到“写入 `tasks.json`”之间存在微秒级延迟，可能导致多个并行任务重复选中同一个空闲 Agent，瞬间造成单点过载。

### 3.2 锁与交接机制
`FACT` Herdr 引入了排他预占锁：
1. **获取排他锁**: 选人逻辑在 `fcntl.flock(lock.fileno(), fcntl.LOCK_EX)` 保护下执行。
2. **写预占记录**: 选中 Agent 后，立即在 `agent-reservations.json` 写入带时间戳的 Reservation。
3. **300 秒 TTL 自愈**:
   - `_clean_reservations` 每次遍历时，检查 Reservation 的 `created_at`。
   - 若某任务超过 300 秒仍未写入 `tasks.json`（例如 worker 进程中途崩溃），该预占锁强制自动释放，防止全局死锁。
4. **所有权平滑交接**:
   - 一旦 Task 成功注册到 `tasks.json`，清理函数检测到 `task_id in registered`，立刻将其从 reservations 中移除，无缝交接给 `tasks.json` 维持活跃计数。

Evidence:
- `herdr/agent_router.py#_clean_reservations`
- `herdr/agent_router.py#release_agent_reservation`
- `RULES.md:并发与死锁防御`

---

## 4. 门禁防御：Deep Preflight 强制健康检查

> [!WARNING]
> **No READY Agent 路由阻断机制**  
> 如果工作流实例记录了 `healthy_agents`，选人系统将强制拒绝任何未出现在健康列表中的 Agent。若所有 Agent 均因 Token 耗尽或登录失效而未通过体检，路由将主动阻断抛错，防止向不可用环境盲目派发任务造成状态卡死。

### 4.1 减法优先与快照时效（2026-09-17 路由黑洞事故后加固）

`FACT` 健康门禁的候选过滤链为 **`allowed - disabled - unhealthy`** 的减法优先，再叠加正向交集：

1. **`unhealthy_agents` 永不自动入选**：即使 `healthy_agents` 为空（冷启动）或体检同时给出正向名单，
   workflow 记录中明确标记为不健康（如 `pi: AUTH_REQUIRED`、`codex: TOKEN_EXHAUSTED`）的 Agent
   一律不进入自动候选；显式 `--agent <bad>` 仍抛错阻断。
2. **正向名单只在快照新鲜时生效**：`preflight_checked_at` 距今超过 `HERDR_PREFLIGHT_TTL`（默认 1800s）
   时，`healthy_agents` 不再作为"只允许此集合"的限制（避免把任务锁死在数小时前健康、现在可能已全灭
   的旧集合），降级为"仅减去 unhealthy"；时间戳缺失或不可解析视为新鲜，保持 legacy 记录与
   "单 Agent 环境优雅回退"语义不变。

背景：`wf-nexusarchive-0917-01` 的 test 节点被派给 `pi`（3 秒空完成 → 总指挥判定 failed），
人工 28 分钟后才重新派发 claude；根因是旧逻辑仅在 `healthy` 非空时做交集，坏 Agent 直接漏网。

Evidence:
- `herdr/agent_router.py#choose_agent`
- `herdr/agent_router.py#preflight_snapshot_fresh`
- `tests/test_agent_router_preflight.py`
- `docs/lessons/lessons-learned.md` §60

---

## 5. 投递熔断与基础设施失败自动补派

`FACT` **三条互补的存活保障链路**（详见 [[architecture]] §2.1/§2.2）：

1. **投递熔断（Sentinel）**：任务停留在 `dispatched` 超过 `HERDR_DISPATCH_DELIVERY_SLA`（默认 600s）
   且 Pane 无 `HERDR_ORCH_TASK:<task_id>` 标记、Agent 非 `working` → 置 `failed`
   （reason `dispatch_delivery_fuse`）并推送通知；有标记或已 working 只告警不处置。
   与既有 Nudge 互补：Nudge 覆盖"有标记但假死"，熔断覆盖"投递完全失败无标记"。
2. **基础设施失败自动补派（Controller）**：registry watcher 对 `failed` 任务调用纯选择器
   `select_infra_failures_for_recovery`（仅 `dispatch_delivery_fuse` / `agent_process_crash`，
   节点无活跃任务，谱系次数 < `HERDR_AUTO_RECOVER_MAX`），命中后 supersede 并清节点
   stage-advance 闩，由 sweep 补派 `-rN` 替代任务；质量类失败（测试 FAIL、总指挥判 failed）
   绝不自动翻案。
3. **纯函数 payload 化**：熔断判定与补派选择均在 `herdr/liveness.py` 以零 I/O 纯函数实现，
   便于零成本单测与回归。

Evidence:
- `herdr/liveness.py#evaluate_dispatch_fuse`
- `herdr/liveness.py#select_infra_failures_for_recovery`
- `services/herdr-sentinel.py#check_dispatch_fuse`
- `services/herdr-controller.py#recover_infra_failed_tasks`
- `tests/test_dispatch_fuse.py`

---

## 6. 门禁防御：Deep Preflight 强制健康检查（原始机制）

`FACT` 深度探针在工作流启动时对候选 Agent 做无副作用沙盒体检，结果写入 workflow 记录的
`healthy_agents` / `unhealthy_agents` / `preflight_checked_at`，由 §4.1 的选人链路消费。

Evidence:
- `herdr/preflight.py` / `herdr/deep_preflight.py`
- `CLAUDE.md:坑点 4：No READY Agent found 路由阻断`
- [[preflight-and-health]]

---

## 7. 自适应路由影子层（Adaptive Router v1, Shadow Mode）

`FACT` 每次 `choose_agent` 返回后，影子层基于已结算的
AgentExecutionOutcome 不可变事实计算推荐并持久化 `route_decision` 事件；
实际派发恒为旧 Router 结果，影子异常时记 `route_decision_error` 并
fail-open。Qualified Success 在 Outcome 固化时一次算定（requirements +
verification 双 true 且终态为 completed 类），Router 只读不算；未结算的
Run 永不进入统计；Outcome 查询带 recorded_at cutoff 并排除当前 run，
防未来信息泄漏。

Evidence:
- `herdr/execution_outcome.py`
- `herdr/state_db.py#query_execution_outcomes`
- `herdr/adaptive_router.py`
- `herdr/agent_router.py#choose_agent`
- `tests/test_execution_outcome.py`
- `tests/test_adaptive_router.py`
- `docs/architecture/adaptive-agent-router.md`

---

## 8. Canary 分流（Adaptive Router v2）

`FACT`（PR #103）显式请求 / workflow 覆盖 / node fixed 之外，auto 选路分支可被
Canary 门接管：仅当操作者启用配置（默认关闭，`~/.herdr-controller/route-canary.json`
或 `HERDR_ROUTE_CANARY_CONFIG`，损坏/非法 = 关闭）、推荐 bucket 在白名单、且该
bucket 的 `model_data_status` 与 `evaluation_data_status` 双 `sufficient`（复用
Shadow Evaluation 权威判定，扫描有界）时，`sha256("canary-v2|{run_id}|{task_id}")`
mod 100 < percentage 的身份改派推荐 Agent（目标恒为池/健康/隔离过滤后的候选
成员）。持久化门：真分流的 `route_decision`(mode=canary, diverted) 必须在路由
临界区内、写 reservation 之前落盘成功（No persisted canary decision, no canary
execution），失败即回 Legacy 并把 reservation 记为 Legacy；非分流决策锁外
best-effort 记录。每次路由恰一条 `route_decision`；Canary 路径任何异常 fail-open
回 Legacy 并留 `route_decision_error`。Shadow 评估跳过 canary 事件；观测对比由
只读 `herdr-task canary-eval` 两臂（diverted vs 未分流）完成，臂样本经 #101 的
execution 去重、bucket 按 recommended_agent × node × task_type 划分，只报事实
——扩量是 #104 的人工决策。

Evidence:
- `herdr/canary_router.py` / `herdr/canary_evaluation.py`
- `herdr/shadow_rows.py#_collect_rows_with_meta`（mode 切片单实现）
- `herdr/agent_router.py#choose_agent`
- `tests/test_canary_router.py` / `tests/test_canary_evaluation.py`
- `docs/architecture/adaptive-router-canary.md`

---

## 9. 受控扩量（Adaptive Router Controlled Rollout, v1）

`FACT` Canary 分流比例从哪来：`herdr/rollout_policy.py` 是唯一的 rollout 状态
管理者，按 `recommended_agent × node × task_type` 维护闭枚举阶段
`off/5/10/25/50`（无 75/100、无任意整数）。Router 只消费它给出的
`effective_percentage`；#103 的 `sha256("canary-v2|run|task") mod 100` 身份、
白名单、准入、持久化门全部不动，因此 `5% ⊂ 10% ⊂ 25% ⊂ 50%` 单调包含、升级不
洗牌。

扩量是人工动作：只能经 `herdr-task rollout set` 相邻推进（`off→5→10→25→50`，
`--reason` 必填，跨级拒绝）；回退可从任意阶段直达 `off`。没有任何自动扩量代码
路径 —— Safety Guard 只写“下”，不写“上”。当前状态与历史事实分离：
`state_db.rollout_state`（当前值）与 `rollout_audit`（不可变，每次变化恰好一条）
在同一个 `BEGIN IMMEDIATE` 事务内写入，败者拿到冲突错误，不存在无审计的变化或
旧状态覆盖新状态。`rollout_enabled` 未设置即生效，因此“空 rollout + 无 canary
配置”仍是 0（默认关闭）；`HERDR_ADAPTIVE_ROLLOUT_ENABLED=false` 立即全 bucket
Legacy 且保留全部历史。解析失败/损坏/非法阶段/DB 异常一律 0 —— rollout 控制失败
永远不能让生产路由更激进。Safety Guard 只读消费 `canary_evaluation` 两臂 facts，
阈值集中可调、样本不足安静、可关闭；自动止损默认由
`herdr-task rollout check-guard --auto-rollback` 执行，每次派发的热路径 guard
需显式 `HERDR_ROLLOUT_HOT_GUARD=1` 开启（用延迟换即时止损）。

Evidence:
- `herdr/rollout_policy.py`
- `herdr/state_db.py#apply_rollout_stage_atomic` / `#_ensure_rollout_schema`
- `herdr/canary_router.py#plan_canary`（只消费 `effective_percentage`）
- `tests/test_rollout_policy.py`
- `docs/architecture/adaptive-router-rollout.md`
