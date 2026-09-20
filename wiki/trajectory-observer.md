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
| `herdr/observer/context.py` | Core/IO | `read_log_tail`（bytes→lines→chars，读取时脱敏）与 `build_observation_context`（预算收紧） |
| `herdr/observer/engine.py` | Core | `TrajectoryObserver.observe_run`：检测→确认→去重→落库；顶层 fail-safe |
| `herdr/observer/harness.py` | Shell | `observe_run` 公共 API、provider 记忆化、`ObservationScheduler`（daemon 线程、非阻塞、in-flight 去重） |
| `services/herdr-controller.py` | Shell | registry_watcher 对 `working/rework/blocked` 任务 `submit_observation`（非阻塞） |
| `bin/herdr-task#observe` | Shell | 人工入口：`--task-id`/`--run-id`/`--json`/`--no-model` |
| `herdr/state_db.py` | Store | `trajectory_findings` 表 + `record/get/list_trajectory_finding(s)`（UNIQUE finding_key） |

## 3. 确定性 signal 与 Finding 语义

| signal | 可靠事实 | finding_type | 需模型确认 |
| :--- | :--- | :--- | :--- |
| A | 活跃 Run 且 `now-last_event ≥ stall_after_seconds`（默认 1800s） | `stalled_execution` | 是（时长单独永远只给 warning） |
| B | 尾部连续 `verification_completed.passed=false ≥ 2`（任务非 done-claim） | `repeated_failure` | 否（模型可否决） |
| C | `runtime.status=unavailable` 且 Run/Task 未终态 | `runtime_unavailable` | 否 |
| D | `rework ≥ 3` 且首次 rework 后无成功验证、无产物事件 | `no_progress` | 是 |
| E | 相同 action 签名连续失败 ≥ 3（仅当存在 action 事件；V1 无生产者，接口保留） | `repeated_action` | 否 |
| F | 最新验证失败但 Run/Task 已宣告完成或 agent_done | `verification_failure` | 否 |
| G | bounded 日志尾部同一错误签名重复 ≥ 3 次 | `possible_context_problem` | 是 |

`severity`：`info`=值得记录；`warning`=可能浪费或失败；`critical`=很可能已无法正常完成。`recommended_action` 只是建议枚举（continue/inspect/replan/retry/change_agent/request_human/interrupt），**V1 绝不执行**。`other` 枚举保留但 V1 不产出（无法可靠分类时不猜）。

## 4. 有界与脱敏（红线）

1. **有界**：只送最近 N（默认 50）条 trajectory + 最近 5 条 verification + 终止/起始事件；日志只读尾部（默认末 16KB→末 200 行→≤4000 字符）；序列化预算 `max_context_size`（默认 8000 字符），超预算按 logs→artifacts→verification→recent→signals 顺序收缩。
2. **脱敏**：日志在 `read_log_tail` 读取时即 `redact_text`；Provider question、`metadata.facts`、evidence excerpt/signature 落库前再做防御性脱敏；密钥形状内容不离开进程。
3. **模型边界**：Jev 契约仅支持 noul/score/choice，Observer 采用「规则检测 → noul 批量确认/否决」映射，不解析自由文本；`requires_confirmation=false` 的证据型 signal 在 Provider 不可用时仍产出，弱 signal 无确认则不产出（宁可不报）。
4. **去重**：`finding_key = sha256(run_id|finding_type|node|agent_session_id|anchor)`，anchor 是本次问题 episode 的稳定起点；SQLite `UNIQUE(finding_key)` + `ON CONFLICT DO NOTHING`，跨观察周期/进程不重复写入。
5. **失败隔离**：`observe_run` 顶层 try/except 永不外抛；调度器 daemon 线程与 controller 轮询物理隔离；只写 `trajectory_findings` 表，绝不触碰 Task/Workflow/Runtime/events。

## 5. 配置速查

| 键 / env | 默认 | 说明 |
| :--- | :--- | :--- |
| `enabled` / `HERDR_OBSERVER_ENABLED` | true | 总 kill switch（false = 零观察） |
| `provider` / `HERDR_OBSERVER_PROVIDER` | `jev` | 复用 `herdr/decision` registry；无 key/provider 不可用时仅产出证据型 Finding |
| `interval` | 300 | 每个 Run 最小观察间隔（RateGate） |
| `max_calls_per_run` | 24 | 单 Run 观察预算上限 |
| `recent_events` / `verification_events` | 50 / 5 | Provider 窗口大小 |
| `max_context_size` | 8000 | 序列化预算（下限 500） |
| `confidence_threshold` | 0.6 | 模型确认阈值 |
| `stall_after_seconds` | 1800 | 停滞 signal 阈值（只产生 warning，除非叠加 ≥3 连续验证失败） |
| `repeated_failure_min` / `repeated_action_min` / `no_progress_min_reworks` / `log_repeat_min` | 2/3/3/3 | 各信号最小事实计数 |
| `log_tail_lines` / `log_tail_bytes` / `log_tail_chars` | 200 / 16384 / 4000 | 日志尾部三级上限 |

## 6. 演进方向（明确不在 V1）

自动 remediation（terminate/retry/replan/换 Agent/修代码/改 Workflow）、ObservationPack、Context Compact、EvidenceReceipt、Dashboard、长期趋势、Agent 评分、Self-improving Harness、Finding 生命周期状态机（V1 status 恒为 `open`）。

Evidence:
- `herdr/observer/models.py:TrajectoryFinding, finding_key_for`
- `herdr/observer/signals.py:detect_signals, Signal, question_for`
- `herdr/observer/context.py:read_log_tail, build_observation_context`
- `herdr/observer/engine.py:TrajectoryObserver`
- `herdr/observer/harness.py:observe_run, ObservationScheduler, submit_observation`
- `herdr/state_db.py:record_trajectory_finding, list_trajectory_findings`
- `services/herdr-controller.py:registry_watcher`
- `bin/herdr-task:cmd_observe`
- `tests/test_trajectory_observer.py`
- `docs/superpowers/specs/2026-09-20-trajectory-observer-design.md`
