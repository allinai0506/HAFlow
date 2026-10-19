# 0003 Semantic Conflicts — 语义冲突报告（不自行裁决）

规则：遇到文档与代码冲突记录三层语义，不替业务方拍板。Observed = 生产运行证据，本轮未采集（not_collected）。

| ID | 术语 | Intended（文档宣称） | Implemented（代码实际） | Observed | Decision |
|---|---|---|---|---|---|
| SC-001 | 任务状态数量 | `wiki/domain-model.md` L139、`wiki/task-lifecycle.md` L15 称“11 状态机” | `herdr/transitions.py` L19-35 定义 15 状态（含 paused/interrupted/superseded）；mermaid 图仅画 12 个 | not_collected | unresolved |
| SC-002 | 状态机单一来源 | 状态转换矩阵应唯一（wiki 以 `herdr/transitions.py` 为证据） | `bin/herdr-task` L140-156 存在 ImportError 兜底副本，且缺少 `blocked -> rework` 边 | not_collected | unresolved |
| SC-003 | 预占清理语义 | `wiki/agent-routing-and-pools.md` L81 称任务注册后从 reservations 清理 | `_clean_reservations` L272-274 对已注册任务保留预占；释放靠显式 `release_agent_reservation` | not_collected | unresolved |
| SC-004 | 存储权威 | `docs/architecture/architecture-overview.md` L40-47 将 projects.json/workflows.json/tasks.json 列为持久化层 | `herdr/state_store.py` L7-10 明确 JSON 仅 migration/export/compat；权威为 state.db | not_collected | unresolved |
| SC-005 | 门禁打回字段 | Schema/authoring guide 使用 `gate.retry_target` | 运行时 fix-loop 只读 `retry_node`（controller L946-973）；`retry_target` 仅被校验 | not_collected | unresolved |
| SC-006 | Run 快照失败行为 | 模板 Schema L38 称快照失败告警并回退共享文件 | `herdr/projects.py` L573-577 在源文件可读时直接拒绝注册；仅源缺失才回退 | not_collected | unresolved |
| SC-007 | 迁移事件命名 | wiki/规格称 `task_status_changed` 在真实变化时写入 | 权威事件为同事务 `task_transition`（state_db L6820-6833）；`task_status_changed` 是 best-effort 轨迹事件（kernel L203-222），失败可缺失 | not_collected | unresolved |
| SC-008 | active 状态集合 | 词面相同：“活跃任务” | 路由器 active={pending,dispatched,working,blocked,agent_done,rework}（agent_router L300-317）；状态机 ACTIVE 不含 pending、含 paused/interrupted（transitions L37-45）；node_capacity 含 pending | not_collected | unresolved |
| SC-009 | 预占文件名 | `docs/product-specs/agent-policy-spec.md` 写 `router-reservations.json` | 代码 `agent-reservations.json`（agent_router.py L16） | not_collected | unresolved |
| SC-010 | 门禁豁免审批 | 设计意图为人工例外通道 | `bypass_task_gate` 仅校验 reason 非空；operator 为自由字符串；无第二人审批；CLI/Console 路由无鉴权（kernel L832-857；http_server L270-297） | not_collected | unresolved |

处置建议（属人工决策，不在本轮落地）：

1. SC-001/002：把 `herdr/transitions.py` 定为唯一事实源，删除 CLI 兜底副本或由生成器产出；同步 wiki。
2. SC-003/004/009：先修文档；SC-004 需要在架构文档中明确“SQLite 权威、JSON 投影”的后台迁移策略。
3. SC-005/006：确认产品意图是 `retry_node` 还是 `retry_target`，以及快照失败时失败关闭还是降级。
4. SC-008：为不同象限使用不同术语（Routing Load / Lifecycle Active / Capacity Active），禁止同名合并。
5. SC-010：把 bypass 纳入权限模型；至少记录操作者身份来源与二次审批策略。
