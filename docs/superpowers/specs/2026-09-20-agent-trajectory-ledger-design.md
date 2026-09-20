# Agent Trajectory Ledger Design

## Goal

为 HAFlow 建立一个只记录事实的、按 run_id 顺序读取的 Agent Trajectory Ledger，不改变现有 Workflow、Task 状态机、Agent 调度或 Runtime State。

## Existing architecture

HAFlow 已有 SQLite `events` 表及 `StateStore`/`state_db.record_event`，用于 WorkflowEvent、状态转移和 Continuous Evaluation 事件。Task 的当前执行位置保存在 `task["runtime"]`，其中包含 agent、agent_name、agent_session_id、workspace_id、tab_id、pane_id 等真实运行时证据。Task 状态统一经 `herdr/kernel.py::transition_task` 网关推进；Task 注册和 Agent 启动结果在 `bin/herdr-task` 与 `services/herdr-worker.py` 之间完成；`services/herdr-controller.py` 已有真实 `tests_completed` 检查点。

当前不存在 `execution_id`、`workflow_run_id` 或 `task_run_id`，所以第一版把一次 Task 执行作为一个 run，并在任务注册时生成 `run_id`。

## Architecture

新增 `herdr/trajectory.py` 作为唯一 Ledger 适配层。它使用既有 SQLite events 存储，不创建第二个 JSONL 或数据库，也不把历史事件塞进 `runtime_state`。现有事件行增加可空的 `run_id` 和 `sequence` 字段；没有这两个字段的旧 WorkflowEvent 保持可读、可写和原有过滤语义。

`TrajectoryLedger.append_event` 将事件模型转换为已有事件存储格式。写入带 run_id 的事件时，在 SQLite 写事务中取该 run 的最大 sequence 加一；SQLite 的写锁保证并发追加不会得到重复序号。`list_events(run_id)` 只读取该 run 的轨迹事件并按 sequence、event id 升序返回。

## Event schema

```text
event_id: str
run_id: str
task_id: str | None
workflow_id: str | None
timestamp: float
sequence: int
event_type: str
node: str | None
stage: str | None
agent: str | None
agent_name: str | None
agent_session_id: str | None
workspace_id: str | None
tab_id: str | None
pane_id: str | None
status: str | None
action: dict | None
observation: dict | None
artifact: dict | None
verification: dict | None
usage: dict | None
metadata: dict
```

First-version event types written by this change are `run_started`, `task_started`, `task_status_changed`, `agent_started`, `verification_completed`, `task_completed`, `task_failed`, `run_completed`, and `run_failed`. The implementation does not emit action, observation, artifact, tool or usage events without an existing reliable producer.

## Integration and failure behavior

- Task launch creates a `run_id`, persists it in the task payload, and records `run_started`, `task_started`, and `agent_started` only after the worker has returned actual runtime identity.
- `kernel.transition_task` records `task_status_changed` after the existing state transaction returns. Terminal transitions additionally record task/run terminal events. Ledger errors are caught and logged so they cannot alter state transition behavior.
- `check_task_tests_completed` records `verification_completed` only when the existing real `tests_completed` event is recorded. The verification payload contains existing test evidence facts, not raw logs.
- Legacy tasks without a run_id use deterministic `run_<task_id>` when a transition is observed, allowing history without rewriting old task records.
- Optional fields are omitted from serialized events. No empty strings or fabricated runtime identifiers are written.

## Testing

Unit tests cover append/read, sequence order, run isolation, optional-field omission, runtime identity preservation, state transition events, terminal events, and tests_completed verification. Existing state transition, supervisor, and full test suites remain the compatibility gate.

## Non-goals

No Observer Agent, diagnosis, automatic repair/termination, Action Fusion, ObservationPack, Context Compact, EvidenceReceipt, scoring, token optimization, AI summary, self-improving harness, dashboard, or standalone database migration is included.
