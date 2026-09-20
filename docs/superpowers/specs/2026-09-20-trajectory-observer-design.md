# Trajectory Observer V1 Design

## Goal

在既有 Agent Trajectory Ledger 之上，新增一个**旁路、只读、best-effort** 的 Trajectory Observer：给定 `run_id`，读取 Trajectory、Runtime State、已有日志/证据，识别值得关注的问题，输出结构化 `TrajectoryFinding`（Detect + Explain + Recommend），**不执行任何 remediation**，且绝不改变 Workflow / Task / Agent / Runtime / 状态转移。

## Existing architecture

- **事实层**：`herdr/trajectory.py`（`TrajectoryEvent`/`TrajectoryLedger`，PR #69）复用既有 SQLite `events` 表（`source='trajectory'`, `run_id`, `sequence`）；事件类型：`run_started/task_started/agent_started/task_status_changed/verification_completed/task_completed/task_failed/run_completed/run_failed`。
- **RuntimeState**：`task["runtime"]`（agent/agent_session_id/workspace/tab/pane/status），status ∈ created/running/completed/failed/unavailable。
- **Continuous Evaluation**：`herdr/supervisor/`（RateGate 节流、bounded `SupervisorState`、fail-safe harness、Policy Engine），是“观察不改变主链路”的既有范式。
- **Provider 抽象**：`herdr/decision/`（`DecisionProvider.judge/score/choose/judge_many`；`jev` 与 `rule` 两个后端）。Jev 契约仅支持 noul/score/choice，**不支持自由文本/JSON 生成**。
- **日志引用**：`task["evidence"]`（pane 转写落盘文件）与 `~/.herdr-controller/logs/tasks/<task_id>/terminal.log`；supervisor evidence 层已有 bounded tail 读取范式。
- **触发面**：`services/herdr-controller.py::registry_watcher` 轮询循环（1s；已有 `check_task_tests_completed` 观察点）。

## Architecture

```text
TrajectoryLedger.list_events(run_id) ─┐
task["runtime"] / task record ────────┤
bounded log tail ─────────────────────┼─→ ObservationContext (bounded, redacted)
确定性 signal 检测（纯函数）──────────┘         │
                                               ▼
                       DecisionProvider.judge_many（既有抽象，一次批量 noul 确认）
                                               │
                                               ▼
                        TrajectoryFinding（代码生成 summary/severity/action，模型只确认）
                                               │
                                               ▼
                       trajectory_findings 表（与 events 事实表物理分离；finding_key 去重）
```

- 新增 `herdr/observer/` 包：`models`（Finding 模型/枚举/去重键）、`signals`（确定性检测 + 问题模板）、`context`（bounded observation context + bounded 日志尾部）、`config`、`engine`（观察编排）、`harness`（模块级 `observe_run` + 非阻塞调度器）。
- 存储：新增 `trajectory_findings` 表（`herdr/state_db.py`）。**Trajectory Event = fact，Finding = analysis**，分表、分 API、分查询。
- 模型侧：复用 `herdr/decision`，observer 不 import 任何具体后端；Jev 契约限制决定“规则检测 → 模型 noul 确认/否决”的映射，模型不生成自由文本，避免解析器。

## Finding schema

```text
finding_id: str            # fnd_<uuid>
finding_key: str           # 去重键（见 Dedup）
run_id: str
task_id / workflow_id: str | None
created_at: float
finding_type: str          # stalled_execution|repeated_failure|repeated_action|no_progress|
                           # verification_failure|runtime_unavailable|possible_context_problem|other
severity: str              # info|warning|critical
node / agent / agent_session_id: str | None
summary: str               # 确定性模板 + 具体事实（不猜测）
evidence: list[dict]       # 只存引用：event_id/sequence/evidence_id/log ref/excerpt(≤300 字)
suspected_cause: str | None
recommended_action: str    # continue|inspect|replan|retry|change_agent|request_human|interrupt
confidence: float          # 模型确认概率；模型不可用时确定性信号使用 base confidence
status: str                # 恒为 "open"（V1 不做生命周期）
metadata: dict             # provider/anchor/signal facts/window 摘要
```

## Observer input (bounded)

`ObservationContext`（默认预算 8000 字符，`_fit_budget` 同样先砍日志再砍历史）：

```text
run:             run_id/task_id/workflow_id/node/stage/agent/agent_session_id/task_status/runtime_status
runtime:         bounded runtime 字段
window:          total_events/recent_returned/truncated/first-last sequence
recent_events:   最近 N（默认 50）条 trajectory 事件摘要（sequence/event_id/type/ago/status/关键字段）
verification:    最近 K（默认 5）条 verification_completed（passed/evidence_id/计数）
terminal_events: 首个 run_started + 全部终止事件（窗口外也保留，最多 6 条）
artifacts:       事件里的 artifact 引用（V1 无生产者时为空）
logs:            至多 1 个日志引用的 bounded 尾部：末 16KB → 末 200 行 → 4000 字符
signals:         确定性检测结果（含 evidence 引用与事实数字）
```

- 不把整条 Trajectory 送给模型：5000 events 也只送窗口内 50 条 + 关键事件。
- 日志只读尾部，**读取时即脱敏**（`redact_text`），Finding 只存 ref + 必要脱敏 excerpt，不复制日志。
- 所有文本经 `supervisor.state.redact_text` 脱敏（凭据形状不离开进程），Finding 落库前再做一次防御性脱敏。

## Signals and findings (deterministic first)

| signal | 规则（可靠事实） | type | requires_confirmation |
|---|---|---|---|
| A | 活跃 Run 且 `now-last_event ≥ stall_after_seconds`（默认 1800s），无终止事件 | stalled_execution | 是 |
| B | 尾部连续 `verification_completed.passed=false ≥ 2` | repeated_failure | 否（模型可否决） |
| C | `runtime.status=unavailable` 且 Run 未终结且 Task 非终态 | runtime_unavailable | 否 |
| D | `rework` 状态转移 ≥ 3 且首次 rework 后无成功验证、无产物事件、无终止 | no_progress | 是 |
| E | 相同 action 签名连续失败 ≥ 3（仅当存在 action 事件，V1 保留接口） | repeated_action | 否 |
| F | 最新验证失败但 Run/Task 已宣告完成或 agent_done | verification_failure | 否 |
| G | bounded 日志尾部同一错误签名重复 ≥ 3 次 | possible_context_problem | 是 |

- 时间只产生 signal：`stalled_execution` 默认 `warning`，只有叠加 ≥3 次连续验证失败才升 `critical`（禁止仅凭时长判死）。
- 每条 Finding 必须有 evidence；无证据不产出。无法可靠分类时保留 `other` 枚举但 V1 不产出。
- severity 语义：info=值得记录；warning=可能浪费/失败；critical=很可能无法正常完成。
- recommended_action 仅建议，V1 绝不执行。

## Provider mapping

`judge_many({finding_type: "是否存在该问题的明确证据…"}, ObservationContext)`，一次批量请求；`p >= confidence_threshold`（默认 0.6）才确认。模型不可用/失败时：`requires_confirmation=false` 的证据型 signal 仍产出（事实自证），`requires_confirmation=true` 的弱 signal 不产出（宁可不报）。

## Dedup

`finding_key = sha256(run_id | finding_type | node | agent_session_id | anchor)[:20]`；`anchor` 是本次问题“episode”的稳定起点（如 stall 前最后一事件、连续失败链首个失败事件、首次 rework 事件、日志签名）。SQLite `UNIQUE(finding_key)` + `INSERT ... ON CONFLICT DO NOTHING`，跨观察周期、跨进程重启均不重复写入。

## Trigger

- 主入口 `observe_run(run_id, task=..., store=..., provider=..., now=...) -> list[TrajectoryFinding]`（同步，CLI 与测试用）。
- Controller `registry_watcher` 对 `working/rework/blocked` 任务调用非阻塞 `ObservationScheduler.submit`（每 Run 最小间隔 + 调用预算 + in-flight 去重；daemon 线程）；线程内异常/超时/模型失败均被吞掉，主链路零感知。
- 本地 kill switch：`HERDR_OBSERVER_ENABLED=0` / `observer.json` / env 数值覆盖。

## Failure isolation

- `observe_run` 顶层 try/except，任何异常返回既有 findings 或 `[]`，从不抛出。
- 只写 `trajectory_findings` 表；不调用任何 task/workflow 状态 API。
- 调度器线程与 controller 主循环物理隔离（daemon thread），阻塞 ≤ provider timeout 且不影响轮询。

## Non-goals

自动 terminate/retry/replan/换 Agent/改代码/改 Workflow/改 Runtime；Context Compact；ObservationPack；Action Fusion；完整 EvidenceReceipt；Dashboard；长期趋势；Agent 评分；Self-improving Harness；Finding 生命周期状态机。

## Testing

`tests/test_trajectory_observer.py`：正常无 Finding；连续验证失败→repeated_failure；runtime unavailable；Observer 失败不影响 Task/Workflow/事件；重复观察不重复写入；evidence 含真实 event_id/sequence/evidence_id；超长日志 bounded；1000 事件 bounded；另加调度器隔离、kill switch、存储 API、去重键稳定性。
