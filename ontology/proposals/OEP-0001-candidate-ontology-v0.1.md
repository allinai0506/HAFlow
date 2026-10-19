# OEP-0001：HAFlow 候选本体 v0.1（R1 修订）

状态：proposed（REQUEST_CHANGES 已应用 R1，未批准）
类型：新增候选（全部 experimental，无 Breaking Change）
范围：`ontology/` 新增，不触碰业务代码
评审记录：`ontology/proposals/OEP-0001-review-r1.md`

## 1. Observation

HAFlow 的“项目 -> 工作流 -> 节点 -> 任务”闭环语义分散在 Python 代码（herdr/、bin/、services/）、StateDB、JSON 投影、YAML 模板与 Wiki 中。Wiki 与代码已出现漂移（`decisions/0003-semantic-conflicts.md`，19 条登记）。本提案是 Brownfield Discovery 的第一业务切片，目的是把隐式语义显式化，并为长期治理提供可 diff、可门禁的载体。

## 2. Evidence

- 证据清单：`decisions/0002-evidence-inventory.md`。
- 每个正式项的 `evidence` 字段：202 条证据项，记录 kind、source、locator、confidence。
- 机械审计：`ontology/tests/audit_ontology.py`（MAJOR=0、MINOR=0；绑定 28/28；状态机 77/77 边与代码相等）。
- 独立评审与发现：`proposals/OEP-0001-review-r1.md`（5 个 BLOCKER 在候选层已修正，代码层与设计决策仍待人工；20 个 MAJOR 中 10 个已修正，10 个为部分修正或待决策）。

## 3. Current Ontology Gap

- 状态机之外存在 8 类写入路径。在被建模为受控例外或收敛到网关之前，状态机不完整（D1）。
- 写子命令覆盖不足：约 45 个 CLI 写子命令未绑定动作（D7）。
- 门禁身份、工作流初始状态、任务类型词表、fix-loop 集合存在语义冲突（D2–D5）。
- DoD 未满足：黄金用例未执行；无运行时消费者；部分写动作无审计事件；无 Active 动作。
- 动作词表（idempotency、transaction、failure_behavior）为描述性，references/04 未定义枚举（D8）。

## 4. Proposed Diff

相对 `1b2fe9a` 的 R1 修正：

| 类别 | 数量（R1 后） | 主要变化 |
|---|---|---|
| objects | 12 | agent 能力字段名修正；artifact.kind 按代码枚举；门禁与预占描述修正 |
| relations | 12 | 证据修正；groups_task 删除推断 |
| states | 4 | task：out_of_matrix_writers、分类重命名；workflow：initial=running、reopen-workflow；reservation：reserved→removed；gate：补全转移 |
| rules | 14 | 新增 review_closure_required_for_wrapup、task_teardown_allowed；returns 改为 boolean |
| actions | 18 | 修正 launch、start、finalize、cleanup、close、record_gate_verdict、bypass_gate、reserve、release；新增 purge_task、force_pass_gate、rollback_workflow、supersede_task |
| policies | 4 | allowed_actions 由 actions 派生；agent_execution 取值限定为 inherit_user / service_identity |
| connectors | 5 | 操作由绑定派生；移除 user_tokens；timeout 与 retry 标注 unknown |
| mappings | 10 | gate_verdict 映射注明双重权威 |
| projections | 3 | console 与 coordinator 移除写动作；executor 证据修正 |
| workflows | 3 | 步骤加 id；触发器结构化；fix-loop 使用 supersede_task |
| golden cases | 7 | task_happy_path 去除无关动作；reservation 用例改为记录移除 |

全部 85 个正式项为 `experimental`，不构成运行时依赖。

## 5. Breaking Change

- [x] No
- 说明：纯新增，无 Active 项，无运行时依赖。若未来收敛越权写入路径（D1）或修改动作 effects，属于行为变更，需单独 OEP 并走人工评审。

## 6. Impacted Consumers

- 当前：无。没有任何运行时读取 `ontology/`。
- 计划消费者（未接线）：coordinator_workflow_projection、task_executor_projection、console_operator_projection。接线并验证前，不得标记为 active。
- 协调器权限边界未证实（unknown），因此协调器投影不授予任何动作。

## 7. Golden Tests

- 声明 7 例：task_happy_path、task_illegal_transition、dag_cycle_rejected、gate_blocked_requires_note、gate_bypass_audited、reservation_expired_reclaimed、anchor_pane_not_assignable。
- 执行状态：**未执行**（无 runner）。引用的 rule 与 action id 已由审计解析通过。
- 黄金用例的 expected 字段为声明式，尚不能作为回归证据。

## 8. Migration / Compatibility

- 无迁移（新增目录）。
- 内部重命名：状态分类键由 `terminal` 与 `completed` 改为 `lane_closed` 与 `failed_lane`；仅候选内部使用，无消费者。
- 预占状态 `expired` 撤回为“记录移除”（磁盘上不存在持久化的 expired 状态）。

## 9. Human Decision

- Status：pending（REQUEST_CHANGES 已应用 R1，未批准）
- Reviewer：（待人工）
- Decision：（待人工，见 `OEP-0001-review-r1.md` §5 的 D1–D9）
