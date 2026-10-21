# Thin Orchestrator V1 验证记录

日期：2026-10-21
基线：`1ddffd1c13f69d4ffd7bc357c1020280bdca4642`
模板：`general-task-v1`（未修改）

## 结论

Controller 的确定性派发路径已不再把总指挥 Pane 当作首节点或后续节点的前置条件。现有 Router、Worker、StateStore、成果门禁、人工决策及 node-dispatch 恢复合同保持不变。

隔离真实 Agent E2E **未验证完成**。三次隔离尝试证明了首节点可在缺少总指挥 Pane 时进入真实 Worker 路径，也证明了身份、预检和 Router 失败会 fail closed；但环境中的可用 Agent 未能在统一隔离环境内通过并保持真实请求预检，因此没有取得三个节点全部完成的真实证据，不能据此宣称生产级自主执行已得到证明。

## 原问题与修改

| 调用入口 | 原行为 | 证据与判定 | V1 修改 |
| --- | --- | --- | --- |
| `check_workflow_stage_advance` | sweep 在读取 DAG 前要求 `coordinator_pane_for_workflow`；缺失时把 node-dispatch operation 统一 defer 为 `coordinator_missing` 并返回。 | readiness、依赖、门禁与恢复事实均已持久化，不需要 LLM。 | 删除全局 Pane 前置检查；继续执行既有确定性 readiness 与 operation 恢复。 |
| `try_direct_stage_advance` | 同时要求 `project_root` 与 `coordinator_pane_id`，但 Worker launch 不消费 coordinator Pane。 | 技术实现遗留，不是业务依赖。 | 仅保留 `project_root` 及既有启动、节点、门禁、候选和 Router 条件。 |
| `_handle_stage_advance_item` | `stage=start` 默认先进入 coordinator intake；连续失败后才尝试 direct dispatch。 | 完整、无歧义节点不需要总指挥理解。 | 根节点和后续节点均先调用 direct planner；只有 planner 无法证明可安全派发时才走兼容 fallback。 |
| direct-dispatch 失败 | 已有 operation lease、`awaiting_result`、有界 defer/attention 与人工处理；启动后不会再走第二 transport。 | 恢复可确定，业务歧义不可确定。 | 保持现有合同；没有新增默认值、无限重试或第二状态机。 |
| continuation | 处理缺少工作、集成收编等未决事项，并可请求 coordinator 判断。 | 不是正常 A→B→C readiness 路径；部分场景确需 Agent/人工。 | 不修改。 |
| auto verdict | 有效机器 verdict 可自动采纳；缺失、无效或身份不一致时 fail closed。 | 已符合薄编排边界。 | 不修改，仅回归验证。 |

## 自动化验证

RED 用例在基线分别证明：无 Pane direct dispatch 被拒、首节点绕过 direct planner、sweep 因无 Pane 不派发。最小实现后以下专项全部通过：

```text
pytest -q tests/test_direct_stage_dispatch.py \
  tests/test_scheduler_dispatch_e2e.py \
  tests/test_stage_advance_and_supersede.py \
  tests/test_workflow_continuation.py \
  tests/test_dispatch_idempotency.py \
  tests/test_controller_modularity.py \
  tests/test_coordinator_auto_heal.py \
  tests/test_node_dispatch_contract.py

329 passed, 44 subtests passed
```

关键合同：

- `test_direct_dispatch_does_not_require_coordinator_pane`：无 coordinator Pane 仍调用既有 `herdr-task launch`。
- `test_first_stage_prefers_deterministic_direct_dispatch`：首节点 direct planner 成功时不进入 coordinator transport。
- `test_first_stage_falls_back_to_coordinator_for_incomplete_contract`：合同不完整时保留兼容 fallback。
- `test_ready_node_sweep_does_not_require_coordinator_pane`：持久化 sweep 不再统一 defer 为 `coordinator_missing`。
- `test_launch_timeout_is_bounded_and_falls_back`、`test_partial_launch_timeout_reports_partial`：暂时失败与副作用不确定保持有界且不二次派发。
- `test_join_gate_refuses_mismatched_sha`、`test_empty_evidence_fails_closed`、`test_set_status_refuses_verdict_without_completion_evidence`：验收失败不推进。
- `test_missing_coordinator_pane_accumulates_bounded_fallback_attempts`：无法确定性处理时产生有界等待，而非伪造完成。
- `test_non_agent_stage_event_blocks_all_agent_paths` 与 `test_human_decision_attention_blocks_until_explicit_clear`：human/tool/gate 等无原生执行器节点写入 `human_decision_required` attention、清除阶段闩并持续阻塞；只有人工显式清除 decision attention 后才从原节点重评。
- `tests/test_dispatch_idempotency.py` 全模块：重复事件、operation ownership 与重启恢复保持幂等。

## 真实运行尝试

以下是执行时观察记录，不是保留现场的独立验收凭证；隔离现场已在失败后注销。因此它们只能说明阻塞与 fail-closed 行为，不能作为完整 E2E 或精确调用量指标。

### 尝试 1：独立 StateStore 环境变量

- workflow_id：`wf-thin-v1-real-20261021`
- 首节点 task_id：`wf-thin-v1-real-20261021-intake_and_scoping-auto`
- coordinator_pane_id：注册后置空
- 结果：Controller 在无 coordinator Pane 时创建真实 Codex Worker，状态进入 `working`；Agent session `01a15b78-9e90-7b00-85ef-c892224c3def` 实际创建阶段产物。
- 阻塞：Worker 受管命令按安全策略清除 `HERDR_STATE_DB`，完成命令回落到默认 HOME 的 StateStore；checkpoint 因 task/run/epoch 身份不匹配被拒绝。该轮不计为成功 E2E。

### 尝试 2：临时 HOME

- workflow_id：`wf-thin-v1-real-home-20261021`
- 结果：workflow 定义位于系统临时目录，被 `liveness.workflow_is_foreign` 正确排除调度。该轮不计为成功 E2E。

### 尝试 3：非临时隔离 HOME

- workflow_id：`wf-thin-v1-real-20261021`、`wf-thin-v1-real-green-20261021` 等隔离重试记录
- StateStore：`~/.herdr-controller/e2e-thin-v1-home/.herdr-controller/state.db`
- 结果：隔离与身份路径一致，但真实预检无法稳定取得 READY：Codex `codex exec` 超过 45 秒；OpenCode 的受管服务端口 `49374` 已被占用；Qoder/Claude/Agy/Pi/Grok/Kimi 报告未登录或认证失败。
- 安全结果：Router 拒绝派发，operation 从 `pending` 进入既有有界等待；无节点被虚假标记完成。

隔离现场已注销并关闭 Workspace。上述目录仅用于本次证据，未修改生产 Workflow 或 `w19`。

## 指标与边界

| 指标 | 结果 |
| --- | --- |
| 完整三节点真实自动推进 | 未验证（Agent 真实预检阻塞） |
| 自动化三节点/依赖/门禁路径 | 通过现有 Controller 集成测试验证 |
| 缺少 coordinator Pane 的真实首节点派发 | 1 次成功进入真实 Worker |
| 完整流程 coordinator LLM 调用次数 | 无完整流程可计量；未验证 |
| 验收失败与身份不一致 | fail closed，无后续虚假推进 |
| 重复事件与恢复 | 自动化专项通过（含 operation lease 恢复复用）；真实重启链未完成 |

因此，本 PR 能证明“实现层面的确定性派发不再依赖总指挥”，但不能证明当前机器已经完成一次无总指挥的真实三节点交付。复现时先确保至少一个 Agent 的 `herdr-deep-preflight --deep` 为 READY，再按临时 HOME 之外的隔离 runtime root 启动 Factory 与 Controller，并保持两者环境指纹完全一致。

## 仍需总指挥或人工的情况

- 工作流或节点合同缺失，Controller 无法证明可安全派发。
- 真实业务歧义、授权或人工门禁尚未解决。
- continuation 发现缺少工作或集成收编需要判断。
- Worker 不可用、预检失败、启动副作用不确定或有界重试耗尽。

前两类是业务必需边界；后两类混合了业务判断与运行环境/历史实现兼容。本次只解除正常确定性推进中的技术遗留依赖。
