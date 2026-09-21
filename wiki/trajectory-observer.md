# Trajectory Observer（运行过程诊断层）

> 状态：V1 已落地（只观察、只解释、只建议，绝不执行修复）。核心原则：**Ledger 记录事实，Observer 解释事实，Provider 只做确认，HAFlow 决定处置。**

## 1. 定位：事实层与语义监督层之间的诊断层

Trajectory Ledger（[[task-lifecycle]] §1.2）回答「发生了什么」；Semantic Supervisor（[[semantic-supervisor]]）在 checkpoint 上做语义监督与 Policy 决策。Trajectory Observer 填补中间一环：**给定 `run_id`，HAFlow 能回答「这次执行现在是否存在值得处理的卡点」**，并给出有证据、有类型、有严重度、有建议的结构化 Finding。

```text
TrajectoryLedger.list_events(run_id) ─┐
task["runtime"] + task record ────────┤
bounded 日志尾部(existing evidence) ──┼─► ObservationContext(有界/脱敏)
确定性 signal 检测(纯函数) ────────────┘          │
                                                ▼
                     DecisionProvider.judge_many(既有抽象,单次批量 noul 确认)
                                                │
                                                ▼
                     TrajectoryFinding(代码生成 summary/severity/action)
                                                │
                                                ▼
                trajectory_findings 表(与 events 事实表物理分离; finding_key 去重)
```

## 2. 模块地图

| 文件 | 层 | 职责 |
| :--- | :--- | :--- |
| `herdr/observer/models.py` | Core | `TrajectoryFinding`、`FINDING_TYPES`/`SEVERITIES`/`RECOMMENDED_ACTIONS`、`finding_key_for`（去重键） |
| `herdr/observer/config.py` | Core | 默认值 < `~/.herdr-controller/observer.json` < `HERDR_OBSERVER_*` env；`enabled` 即 kill switch |
| `herdr/observer/signals.py` | Core | 7 个确定性 signal 检测器 + Provider noul 问题模板；无 I/O、无副作用 |
| `herdr/observer/context.py` | Core/IO | `read_log_tail`（bytes→lines→chars，读取时脱敏）、`bound_transcript`（文件/实时日志同一边界）、`build_observation_context`（persisted/live runtime 明确区分 + hard budget） |
| `herdr/observer/live.py` | Shell-lite/IO | 只读 live Pane/Agent 探测（`pane list`/`pane get`/`agent get`，失败=unknown）与 live Pane transcript（`pane read --source recent-unwrapped`，timeout + 三级上限 + 脱敏） |
| `herdr/observer/engine.py` | Core | `TrajectoryObserver.observe_run`：`task` 可省略（按 run 事件 `task_id` → 持久化 `run_id` 自动解析 task/runtime/日志）；检测→确认→去重→落库；顶层 fail-safe |
| `herdr/observer/harness.py` | Shell | `observe_run` 公共 API、provider 记忆化、`ObservationScheduler`（daemon 线程、非阻塞、in-flight 去重） |
| `services/herdr-controller.py` | Shell | registry_watcher 对 `working/rework/blocked` 任务 `submit_observation`（非阻塞）；统一 Done Gateway `emit_done_if_allowed` 入口调用 `_observer_terminal_checkpoint`（每 run 每进程一次，覆盖 listener/recovery/redelivery） |
| `bin/herdr-task#observe` | Shell | 人工入口：`--task-id`/`--run-id`/`--json`/`--no-model` |
| `herdr/state_db.py` | Store | `trajectory_findings` 表 + `record/get/list_trajectory_finding(s)`（UNIQUE finding_key） |

## 3. 确定性 signal 与 Finding 语义

| signal | 可靠事实 | finding_type | 需模型确认 |
| :--- | :--- | :--- | :--- |
| A | 活跃 Run 且 `now-last_event ≥ stall_after_seconds`（默认 1800s） | `stalled_execution` | 是（时长单独永远只给 warning） |
| B | 尾部连续 `verification_completed.passed=false ≥ 2`（任务非 done-claim） | `repeated_failure` | 否（模型可否决） |
| C | `runtime.status=unavailable` **或 live Pane/Agent 探测 unavailable**（probe 失败=unknown 不判死） | `runtime_unavailable` | 否 |
| D | 最近一次进展边界（passed verification 或 artifact）之后 `rework ≥ 3` 且无新进展（anchor=当前 episode 首次 rework） | `no_progress` | 是 |
| E | 相同 action 签名连续失败 ≥ 3（仅当存在 action 事件；V1 无生产者，接口保留） | `repeated_action` | 否 |
| F | 最新验证失败但 Run/Task 已宣告完成或 agent_done | `verification_failure` | 否 |
| G | bounded 日志尾部同一错误签名重复 ≥ 3 次 | `possible_context_problem` | 是 |

`severity`：`info`=值得记录；`warning`=可能浪费或失败；`critical`=很可能已无法正常完成。`recommended_action` 只是建议枚举（continue/inspect/replan/retry/change_agent/request_human/interrupt），**V1 绝不执行**。`other` 枚举保留但 V1 不产出（无法可靠分类时不猜）。

## 4. 有界与脱敏（红线）

1. **有界**：只送最近 N（默认 50）条 trajectory + 最近 5 条 verification + 终止/起始事件；日志只读尾部（默认末 16KB→末 200 行→≤4000 字符）；序列化预算 `max_context_size`（默认 8000 字符），超预算按 logs→artifacts→verification→recent→signals 顺序收缩。
2. **脱敏先于截断**：`bound_transcript` 先 strip ANSI + `redact_text`，再执行 bytes/lines/chars 上限；文件尾部先多读 8192B overlap 并丢弃首个不完整行（保证完整行边界）再脱敏、再截断；Provider question、`metadata.facts`、evidence excerpt/signature 落库前再做防御性脱敏；密钥形状内容（含被 cutoff 切断 key prefix 的场景）不离开进程。
3. **模型边界**：Jev 契约仅支持 noul/score/choice，Observer 采用「规则检测 → noul 批量确认/否决」映射，不解析自由文本；`requires_confirmation=false` 的证据型 signal 在 Provider 不可用时仍产出，弱 signal 无确认则不产出（宁可不报）。
4. **去重与升级**：`finding_key = sha256(run_id|finding_type|node|agent_session_id|anchor)`，anchor 是本次问题 episode 的稳定起点；SQLite `UNIQUE(finding_key)` + `ON CONFLICT DO UPDATE`——重复观察不新增第二条 Finding，同一 episode 原地刷新 severity/summary/evidence/原因/建议/置信度（如失败链 2→4 次 warning→critical），`finding_id`/`created_at` 保持 canonical，低 severity 观察不降级既有行；并发冲突后重新读取并返回 canonical persisted finding。
5. **Live 真实性**：persisted `task["runtime"]` 之外增加只读 live 探测，身份优先级 **A** session（`agent_session_id` 必须匹配）→ **B** `agent_name`（具体实例名必须匹配 live `agent.name`）→ **C** 仅 agent type（claude 等）不足以证明 Run ownership → `unknown` 且禁止 `pane read`；pane 级 session 矛盾或显式 `pane_not_found` → `unavailable`。**对 persisted 明确有 Agent 的 Run，pane session 一致不等于 Agent 存活，必须继续 `agent get` 确认**（与 `pane_pool` 判据一致）；agent_not_found/空 agent/身份不一致→`unavailable`；timeout/daemon/parse/身份信息不足一律 `unknown`（绝不当 unavailable）。live Pane transcript 必须通过同一身份 guard（最终 probe `available` 才允许 `pane read`），优先于 finalization 才出现的 `task["evidence"]`，未确认身份或读取失败回退文件；两者都只在 daemon worker 线程执行（probe ≤2s、transcript ≤3s），绝不进入 controller 主轮询。
6. **失败隔离**：`observe_run` 顶层 try/except 永不外抛；Provider 构造失败仅记 stderr 并降级为无 Provider（证据型 Finding 照常产出，弱信号静默）；调度器 daemon 线程与 controller 轮询物理隔离；只写 `trajectory_findings` 表，绝不触碰 Task/Workflow/Runtime/events。
7. **Hard budget**：`_fit_budget` 递归 clamp 嵌套字段并按序删除低优先级块，最终 serialized ≤ `max(500, max_context_size)`；最小 identity（run_id + signal 类型）在任何输入下都保留。
8. **agent_done terminal checkpoint**：挂在统一 Done Gateway `emit_done_if_allowed()` 入口（listener / recovery / registry redelivery / rework heal 全部经此网关，任务推进前必获一次观察机会）；独立 gate 不走 periodic interval/budget（避免被刚发生的 working observation 挡掉），每 run 每 controller 进程最多一次（进程内 seen，TTL 24h；重启重置），daemon 线程异步、失败不阻塞 done flow；受并发上限或 thread 启动失败而未提交时不标记 seen，后续 gateway 调用可重试（任务已推进到终态则可能不再获得机会，best-effort）；registry_watcher 不再单独调用。
9. **CLI 契约**：`--task-id`/`--run-id` 互斥（禁止跨 Run 混用身份）；`--json` 的 stdout 只允许 JSON，诊断全部走 stderr，Provider 失败时仍 exit 0。

## 5. 配置速查

| 键 / env | 默认 | 说明 |
| :--- | :--- | :--- |
| `enabled` / `HERDR_OBSERVER_ENABLED` | true | 总 kill switch（false = 零观察）；**测试套件在 conftest 默认置 0**（避免 done-path 测试经默认调度器写生产状态库） |
| `provider` / `HERDR_OBSERVER_PROVIDER` | `jev` | 复用 `herdr/decision` registry；无 key/provider 不可用时仅产出证据型 Finding |
| `interval` | 300 | 每个 Run 最小观察间隔（RateGate） |
| `max_calls_per_run` | 24 | **process-local** 单 Run 观察预算（RateGate 进程内计数，Controller 重启后重置，V1 不持久化） |
| `recent_events` / `verification_events` | 50 / 5 | Provider 窗口大小 |
| `max_context_size` | 8000 | 序列化预算（**最小 500，load_config 显式 clamp**） |
| `confidence_threshold` | 0.6 | 模型确认阈值 |
| `stall_after_seconds` | 1800 | 停滞 signal 阈值（只产生 warning，除非叠加 ≥3 连续验证失败） |
| `repeated_failure_min` / `repeated_action_min` / `no_progress_min_reworks` / `log_repeat_min` | 2/3/3/3 | 各信号最小事实计数 |
| `log_tail_lines` / `log_tail_bytes` / `log_tail_chars` | 200 / 16384 / 4000 | 日志尾部三级上限（文件与 live transcript 共用） |
| `live_probe` / `HERDR_OBSERVER_LIVE_PROBE` | true | 只读 live Pane/Agent 探测开关（false = 只用 persisted runtime） |
| `live_probe_timeout` / `live_transcript_timeout` | 2.0 / 3.0 | live 子进程显式超时（秒） |

## 6. 演进方向（明确不在 V1）

自动 remediation（terminate/retry/replan/换 Agent/修代码/改 Workflow）、ObservationPack、Context Compact、EvidenceReceipt、Dashboard、长期趋势、Agent 评分、Self-improving Harness、Finding 生命周期状态机（V1 status 恒为 `open`）。

Evidence:
- `herdr/observer/models.py:TrajectoryFinding, finding_key_for`
- `herdr/observer/signals.py:detect_signals, Signal, question_for`
- `herdr/observer/context.py:read_log_tail, bound_transcript, build_observation_context`
- `herdr/observer/live.py:probe_live_runtime, read_live_transcript`
- `herdr/observer/engine.py:TrajectoryObserver`
- `herdr/observer/harness.py:observe_run, ObservationScheduler, submit_observation`
- `herdr/state_db.py:record_trajectory_finding, list_trajectory_findings`
- `services/herdr-controller.py:registry_watcher`
- `bin/herdr-task:cmd_observe`
- `tests/test_trajectory_observer.py`
- `docs/superpowers/specs/2026-09-20-trajectory-observer-design.md`
