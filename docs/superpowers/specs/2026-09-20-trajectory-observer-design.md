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
runtime:         明确区分 persisted（task["runtime"]）与 live（只读 Pane/Agent 探测：
                 available/unavailable/unknown + reason + agent_status；probe
                 失败一律 unknown，不得据此判 unavailable）
window:          total_events/recent_returned/truncated/first-last sequence
recent_events:   最近 N（默认 50）条 trajectory 事件摘要（sequence/event_id/type/ago/status/关键字段）
verification:    最近 K（默认 5）条 verification_completed（passed/evidence_id/计数）
terminal_events: 首个 run_started + 全部终止事件（窗口外也保留，最多 6 条）
artifacts:       事件里的 artifact 引用（V1 无生产者时为空）
logs:            至多 1 个日志引用的 bounded 尾部：live Pane transcript 优先，
                 `task["evidence"]` / terminal.log 兜底；末 16KB → 末 200 行 → 4000 字符
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
| C | `runtime.status=unavailable` **或 live Pane/Agent 探测 unavailable**（probe 失败=unknown，不判死），且 Run 未终结且 Task 非终态 | runtime_unavailable | 否 |
| D | 最近一次进展边界（passed verification 或 artifact）之后 `rework ≥ 3` 且无新进展 | no_progress | 是 |
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

`finding_key = sha256(run_id | finding_type | node | agent_session_id | anchor)[:20]`；`anchor` 是本次问题“episode”的稳定起点（如 stall 前最后一事件、连续失败链首个失败事件、首次 rework 事件、日志签名）。SQLite `UNIQUE(finding_key)` + `INSERT ... ON CONFLICT DO UPDATE`：同一 episode 重复观察不新增第二条 Finding，而是原地刷新 severity/summary/evidence/suspected_cause/recommended_action/confidence/metadata（证据升级，如同一条失败链 2→4 次则 warning→critical），`finding_id`/`created_at` 保持 canonical；低 severity 的观察永不降级既有行。并发冲突（两个进程同时对同一 key 写入）后必须重新读取并返回 canonical persisted finding，绝不返回未持久化的本地 finding。

## Trigger

- 主入口 `observe_run(run_id, task=..., store=..., provider=..., now=...) -> list[TrajectoryFinding]`（同步，CLI 与测试用）。`task` 可省略：Observer 先按 run 事件中的 `task_id`（并以 `run_id` 匹配守卫，绝不借用重派前旧 run 的 task）、再按持久化 task 的 `run_id` 匹配自动解析 task，从而读取 Runtime State 与日志，调用者只需 `run_id`；显式传入的 task 若 `run_id` 不匹配同样被忽略。
- Controller `registry_watcher` 对 `working/rework/blocked` 任务调用非阻塞 `ObservationScheduler.submit`（每 Run 最小间隔 + **process-local per-run observation budget**——RateGate 仅进程内计数，Controller 重启后重置，V1 不新增任何持久化计数——+ in-flight 去重；daemon 线程）；线程内异常/超时/模型失败均被吞掉，主链路零感知。live pane 探测与 transcript 读取只在 daemon worker 线程执行（显式 timeout：probe 默认 2s、transcript 默认 3s），绝不进入 controller 主轮询线程。Provider 构造失败只记 stderr 诊断并降级为无 Provider：`requires_confirmation=false` 的证据型 Finding 照常产出，弱信号保持静默。
- **agent_done terminal checkpoint**：`verification_failure` 的核心场景恰是「最新验证失败 + task.status=agent_done」。terminal trigger 挂在**统一 Done Gateway `emit_done_if_allowed()`** 的入口（listener / recovery / registry redelivery / rework heal 全部 done 路径都经此网关），保证在任务推进前获得一次观察机会；该 trigger 使用独立 gate（不走 periodic interval/budget，避免被刚刚发生的 working observation 挡掉），每个 run 在一个 controller process 内最多执行一次（进程内 seen 集合，TTL 24h 有界；重启后重置），仍为 daemon 线程异步执行，失败只记日志、绝不阻塞既有 done flow。thread 启动失败会撤销 seen 标记，后续 gateway 调用可重试（若任务已推进到终态则可能不再获得观察机会——best-effort 语义）。`registry_watcher` 不再单独调用（避免重复职责）。
- CLI `herdr-task observe` 的 `--task-id` 与 `--run-id` 互斥（argparse mutually exclusive），禁止混合两个 Run 的身份；`--json` 模式下 stdout 只允许输出 JSON，所有诊断（Provider 失败/live probe 跳过等）走 stderr，退出码仍为 0。
- 本地 kill switch：`HERDR_OBSERVER_ENABLED=0` / `HERDR_OBSERVER_LIVE_PROBE=0` / `observer.json` / env 数值覆盖。

## Live Runtime 与 Live Transcript

- **Live Runtime**（`herdr/observer/live.py`，只读复用既有 herdr CLI 能力）：
  - pane 存在性：`herdr pane list --workspace <ws>` 验证 daemon/workspace 可达，`herdr pane get <pane>` 给出最终事实——**显式 `pane_not_found` 才是 unavailable**；
  - **身份优先级**（persisted runtime 的 `agent_session_id`/`agent_name`/agent type vs live `pane.agent_session` / `agent.agent_session` / `agent.name`）：
    - **A** persisted `agent_session_id` 存在 → 必须与 live agent session 匹配；
    - **B** 无 session 但 persisted `agent_name`（Herdr 具体实例名）存在 → 必须与 live `agent.name` 匹配；
    - **C** 只有 agent type（claude/opencode 等）→ **不足以证明 Run ownership** → `unknown`（且禁止读取 live transcript）；绝不能因为 persisted=claude 且 live=claude 就判定同一 Run；
    - pane 级 session 已矛盾（都存在但不一致）→ `unavailable`（`identity_mismatch`），无需再问 agent；
    - **对 persisted 明确有 Agent 的 Run，pane session 一致不等于 Agent 存活**（HAFlow `pane_pool` 以 `herdr agent get` 为真实 live agent 判据）：继续 bounded `agent get` 确认——agent 成功且身份匹配 → `available`；agent 显式 `agent_not_found` / 空 agent → `unavailable`；身份不一致 → `unavailable`（`identity_mismatch`）；
    - 未持久化任何 Agent 身份的任务（纯 pane 目标）才允许以 pane 存在 + live session 判 `available`（`pane_alive`；pane 缺 workspace 枚举时附 `workspace_mismatch`）；
    - **timeout / daemon error / parse error（含 agent 响应不可解析或 schema 不符）/ 无法获得足够身份信息 → `unknown`，绝不当作 unavailable**；
  - live transcript 读取前必须通过同一身份 guard：只有 `available` 才允许 `herdr pane read`；`identity_mismatch` / `unavailable` / `unknown` 一律不读当前 Pane（可回退 persisted evidence / terminal.log），防止旧 `pane_id` 复用后读到其他 Run 的日志；
  - 不写 Task status，不写 persisted RuntimeState；输入中 persisted 与 live（含 `agent_session_id`/`workspace_mismatch`）明确分开。
- **Live Transcript**：daemon worker 内以 `herdr pane read <pane> --source recent-unwrapped --lines N`（timeout 3s）读取当前 Pane；**先 strip ANSI + redact，再做 bytes/lines/chars 三级上限**（脱敏必须看到完整语义边界，byte cutoff 不得先于脱敏切断 `api_key=...` 的 key prefix）；无 Pane、身份未确认（`unknown`/`unavailable`）或读取失败时回退既有 `task["evidence"]` / terminal.log；完整 transcript 永不落库、永不整体送 Provider。
- **文件尾部读取**：先多读 `LOG_OVERLAP_BYTES`（8192）overlap 并丢弃首个不完整行（保证从完整行边界开始），随后 redact，最后执行正式 byte/line/char 上限；不读取整个大文件；无法建立完整行边界时跳过该候选。最终 context 大小限制不变。

## Failure isolation

- `observe_run` 顶层 try/except，任何异常返回既有 findings 或 `[]`，从不抛出。
- 只写 `trajectory_findings` 表；不调用任何 task/workflow 状态 API；live probe 只读且失败归 `unknown`。
- 调度器线程与 controller 主循环物理隔离（daemon thread），阻塞 ≤ provider timeout + live probe/transcript timeout，且不影响轮询。
- **Hard budget**：`_fit_budget` 递归 clamp 所有 nested 字段（drop logs/artifacts/verification/terminal → 收缩 recent_events → 收缩 signals → 递归字符串减半 → 最小 identity `run_id` + signal 类型）；无论输入多恶意，`json.dumps(context, ensure_ascii=False)` 最终长度必定 ≤ `max_context_size`。产品最小值为 **500**：`load_config` 显式 clamp（配置里不会出现 100、内部却用 500），`_fit_budget` 只保留同值防御性下限。

## Non-goals

自动 terminate/retry/replan/换 Agent/改代码/改 Workflow/改 Runtime；Context Compact；ObservationPack；Action Fusion；完整 EvidenceReceipt；Dashboard；长期趋势；Agent 评分；Self-improving Harness；Finding 生命周期状态机。

## Testing

`tests/test_trajectory_observer.py`：正常无 Finding；连续验证失败→repeated_failure；runtime unavailable（persisted 与 live 两条路径，probe 异常/unknown 不得误报）；Observer 失败不影响 Task/Workflow/事件；重复观察不重复写入；证据升级原地更新且 finding_id 不变、不降级；no_progress episode 边界（历史 passed verification/artifact 不屏蔽新 episode）；Provider 构造失败仍产出证据型 Finding；**agent_done terminal checkpoint**（working 刚 observe 过 <300s + verification failed + agent_done → terminal 仍提交并产出 verification_failure；每进程每 run 一次；失败不阻塞 done flow）；**脱敏先于 cutoff**（byte cutoff 落在 `api_key=...` 中间时不泄漏到 excerpt/Provider/Finding/SQLite）；**身份优先级 A/B/C**（type-only 跨 Run 场景 → unknown、不读 pane）；evidence 含真实 event_id/sequence/evidence_id；live transcript 命中/优先/回退/超长截断/密钥不外泄；超长日志 bounded；1000 事件 bounded；恶意嵌套字段 hard budget；CLI `--json` stdout 纯 JSON（Provider 失败仍 exit 0）；`--task-id/--run-id` 互斥与 run 身份不串用；调度器隔离（含慢 live probe 不阻塞 submit）、kill switch、存储 API、去重键稳定性。
