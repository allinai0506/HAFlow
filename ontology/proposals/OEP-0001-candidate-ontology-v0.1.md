# OEP-0001 候选本体 v0.1 提案（待人工评审）

状态：proposed
类型：新增（MINOR，全部 experimental，无 Breaking Change）
范围：`ontology/` 新增，不触碰业务代码

## 为什么提交

把 HAFlow “项目 -> 工作流 -> 节点 -> 任务”闭环的隐式语义显式化，让后续 Agent 与开发者读取同一套定义，
并为长期语义漂移（见 decisions/0003）建立可 diff、可门禁的治理载体。

## 本次包含

- 12 Objects / 12 Relations / 4 States / 12 Rules / 14 Actions / 4 Policies / 5 Connectors / 10 Mappings / 3 Projections / 3 Workflows / 7 Golden Cases
- 全部证据化（kind/source/locator/confidence）
- 全部 `experimental`：不构成运行时依赖，任何运行时引用必须等待评审

## 运行时绑定（摘要）

| Action | Binding |
|---|---|
| provision_project / start_workflow | `herdr-factory` CLI + `herdr.projects` |
| launch_task / transition_task / verify / commit / integrate / finalize / cleanup / close-workflow | `bin/herdr-task` CLI 对应子命令 |
| record_gate_verdict / bypass_gate | `herdr-task set --verdict` / `herdr.kernel.bypass_task_gate` |
| reserve_agent / release_agent_reservation | `herdr.agent_router.choose_agent` / `release_agent_reservation` |
| 状态仲裁 / auto-accept / fix-loop | `services/herdr-controller.py` |

## Breaking Changes

无（纯新增）。注意：一旦本 Ontology 进入 Phase 5.5 impact gate，SC-001..SC-010 对应的代码/文档变更会被判为
语义影响（可能需要 `.ontology-impact.yaml` 声明），但这属于门禁启用后的策略，不是本次提案的破坏性变更。

## Open Questions

1. `workflow_instance.current_stage` 无生产写入方（unknown），是否保留字段或标记 deprecated？
2. `gate.retry_target` 与 `retry_node` 哪个是目标语义（SC-005）？
3. `workflow_definition` 快照失败应 fail-closed 还是降级（SC-006）？
4. Task `paused` 状态的产生方与恢复语义未完全枚举（未找到完整触发清单）。
5. `worker_policy.permissions` 仅在 `herdr/mcp.py` 矩阵中存在，生产调用方未确认接线（declared-only 风险）。
6. 门禁豁免是否需要身份认证与二次审批（SC-010）？
7. `artifact`（投影）与 `event`/`checkpoint`（基础设施型对象）是否应长期保留为 Object？

## Recommended Next Slice

推荐下一片：**证据链语义层** `Trajectory -> Observation -> Finding -> ContextPack`（含 WorkingContext 编译），
理由：它承接本轮 Task/Event 事实，直接对应 AGENTS.md 的语义分层约束，且同样存在文档/代码漂移风险。
备选：Console 运维操作与权限边界（把 SC-010 的缺口产品化）。

## 验证

见 `generated/validation.md`（ontology_validate.py / ontology_index.py 运行记录）。
