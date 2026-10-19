# 0003 Semantic Conflicts — 语义冲突登记（R1 更新）

规则：文档与代码冲突只记录三层语义，不替业务方裁决。Observed 为生产运行证据，本轮未采集（not_collected）。
状态取值：确认、部分确认、撤回、未验证。撤回项保留编号以便追溯。

| ID | 术语或位置 | Intended（文档宣称） | Implemented（代码实际） | Observed | 状态 |
|---|---|---|---|---|---|
| SC-001 | 任务状态数 | wiki/domain-model.md L139、wiki/task-lifecycle.md L15 称“11 状态”；mermaid 图画 12 个节点 | herdr/transitions.py L19-35 定义 15 个状态 | not_collected | 确认 |
| SC-002 | 状态矩阵单一来源 | wiki 以 herdr/transitions.py 为证据 | bin/herdr-task L140-156 兜底副本缺 `blocked→rework`，仅在 ImportError 时生效 | not_collected | 确认 |
| SC-003 | 预占注册后清理 | wiki/agent-routing-and-pools.md L81：注册后从 reservations 移除 | herdr/agent_router.py L272-274 同样移除（continue 丢弃） | — | **撤回**：文档与代码一致（评审 X-01） |
| SC-004 | 存储权威 | docs/architecture/architecture-overview.md L40-47 将 tasks/projects/workflows.json 列为存储层 | herdr/state_store.py L7-10：JSON 仅 export 与 compat，权威为 state.db | not_collected | 确认 |
| SC-005 | 门禁打回字段 | docs/product-specs/workflow-template-schema.md L96 与 authoring guide 使用 `retry_target` | 运行时读取 `retry_node`（controller L946-973）；`retry_target` 仅被校验；shipped 模板 business-research-v1.yaml L67 使用 `retry_target` | not_collected | 确认 |
| SC-006 | Run 快照失败行为 | schema L38 称告警并回退共享文件 | herdr/projects.py L573-577：源文件可读时直接拒绝注册，仅源缺失时回退 | — | 确认 |
| SC-007 | 状态事件命名 | wiki/task-lifecycle.md L119 称 `task_status_changed` 在真实变化时写入 | 权威事件为同事务 `task_transition`（state_db.py L6820-6833）；`task_status_changed` 为 best-effort 轨迹事件（kernel.py L203-222），可能缺失 | — | 确认 |
| SC-008 | “active”集合 | 同一词面 | 路由 active = {pending, dispatched, working, blocked, agent_done, rework}（agent_router.py L304-307）；状态机 ACTIVE 含 paused、interrupted，不含 pending（transitions.py L37-45） | — | 确认（同名不等于同义） |
| SC-009 | 预占文件名 | docs/product-specs/agent-policy-spec.md L54、docs/operations/troubleshooting-faq.md L55、L58 写 `router-reservations.json` | herdr/agent_router.py L16 为 `agent-reservations.json` | — | 确认 |
| SC-010 | 门禁豁免审批 | 设计意图为人工例外通道 | herdr/kernel.py L850-857 仅校验 reason；operator 为自由字符串；无二次审批；控制台写路由无鉴权 | — | 确认 |
| SC-011 | 工作流初始状态 | herdr/transitions.py L89-97 以 pending 为起点 | herdr/projects.py L593 直接写 running；herdr/state_db.py L1617 占位工作流写 pending | — | 确认 |
| SC-012 | 门禁身份与权威 | 文档：门禁为节点级 GateSpec | 结论按任务：tasks.stage_verdict；verdict 文件按 task_id（kernel 写 gate_verdict_path(task_id)）；覆盖（force-pass）按节点 | — | 确认 |
| SC-013 | 任务类型词表 | schema L53：default_task_type = explore/plan/code/test/review/docs | launch --task-type = fix/feat/refactor/docs/test/chore/perf/ci（bin/herdr-task L8823-8835）；domain 注册表 `_SOFTWARE_TASK_TYPES` 另含 bugfix、hotfix、impl、craft | — | 部分确认（模板取值未核对） |
| SC-014 | fix-loop 可作废集合 | controller 注释要求与矩阵“→ superseded”一致 | `FIX_LOOP_SUPERSEDEABLE`（controller L948-958）缺 paused、interrupted；矩阵允许二者转为 superseded | — | 确认 |
| SC-015 | 门禁豁免守卫 | 控制台 force-pass 带 expected_version（console/control_api.py L409） | CLI 与 factory 的 force-pass 不带 expected_version（bin/herdr-factory L652-656）；bypass 亦不带 | — | 确认（控制台确认提示未核对） |
| SC-016 | resume 目标 | 矩阵：paused 不可达 dispatched、blocked | bin/herdr-task L8000-8010 按 status_history 回退到 dispatched 或 blocked；set 对非法边的实际行为未验证 | — | 未验证 |
| SC-017 | `herdr-task add` 覆盖写 | — | bin/herdr-task L1199-1203 与 state_db.py L1714-1731 的覆盖写可能回退并发状态（子代理取证） | — | 未验证 |
| SC-018 | 越权状态变更拦截 | wiki/task-lifecycle.md L15：“任何越权状态变更将被 CLI 直接拦截” | 8 类状态机外写入路径，见 states/task_lifecycle.yaml 的 out_of_matrix_writers | — | 确认 |
| SC-019 | 控制台 force-review | 控制台写路由用于人工复审 | console/task_api.py L266-290 直接设置 status=agent_done，无守卫、无事件 | — | 确认 |

## 处置建议（属人工决策，不在本轮落地）

1. SC-001、SC-002：以 herdr/transitions.py 为唯一事实源，删除 CLI 兜底副本或由生成器产出；同步 wiki。
2. SC-003 已撤回：无需处置。
3. SC-004、SC-009：先修文档；SC-004 需在架构文档中写明“SQLite 权威、JSON 投影”的过渡策略。
4. SC-005、SC-006：确认产品意图是 `retry_node` 还是 `retry_target`；快照失败时失败关闭还是降级。
5. SC-008：为不同含义使用不同术语（路由负载、生命周期活跃、容量活跃），禁止同名合并。
6. SC-010、SC-015、SC-019：把豁免与人工覆盖纳入权限模型，统一守卫，并为控制台写路由增加鉴权（代码变更）。
7. SC-011、SC-012：决定工作流初始状态与门禁权威（D2、D3）。
8. SC-013、SC-014：统一任务类型词表；补齐 fix-loop 可作废集合（D4、D5）。
9. SC-018：决定越权写入是收敛到网关，还是显式建模为受控例外（D1）。
