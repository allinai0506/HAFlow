# 0003 Semantic Conflicts — 语义冲突登记（R1 更新）

规则：文档与代码冲突只记录三层语义，不替业务方裁决。Observed 为生产运行证据，本轮未采集（not_collected）。
状态取值：确认、部分确认、撤回、未验证。撤回项保留编号以便追溯。

| ID | 术语或位置 | Intended（文档宣称） | Implemented（代码实际） | Observed | 状态 |
|---|---|---|---|---|---|
| SC-001 | 任务状态数 | wiki/domain-model.md L139、wiki/task-lifecycle.md L15 历史文档称“11 状态” | herdr/transitions.py L19-35 定义 15 个状态；wiki 已同步更新对齐 | — | **已解决**（对齐为 15 状态） |
| SC-002 | 状态矩阵单一来源 | wiki 以 herdr/transitions.py 为证据 | bin/herdr-task 已删除历史兜底副本，统一直接引用 herdr/transitions.py | — | **已解决**（单一来源确立） |
| SC-003 | 预占注册后清理 | wiki/agent-routing-and-pools.md L81：注册后从 reservations 移除 | herdr/agent_router.py L272-274 同样移除（continue 丢弃） | — | **撤回**：文档与代码一致（评审 X-01） |
| SC-004 | 存储权威 | docs/architecture/architecture-overview.md L40-47 历史版本将 tasks/projects/workflows.json 列为存储层 | herdr/state_store.py L7-10：JSON 仅 export 与 compat，权威为 state.db；architecture-overview 已更新校准 | — | **已解决**（文档校准为 SQLite 权威、JSON 兼容投影） |
| SC-005 | 门禁打回字段 | docs/product-specs/workflow-template-schema.md 历史上使用 `retry_target` | 运行时与 Controller 原只读取 `retry_node`；已收敛统一为 `retry_node`（标准推荐）与 `retry_target`（等价兼容别名），DAG 校验与调度策略均双向支持 | — | **已解决**（双向透明兼容） |
| SC-006 | Run 快照失败行为 | schema L38 历史版本称告警并回退共享文件 | herdr/projects.py L573-577：源文件可读时直接拒绝注册（fail-closed），仅源缺失时回退；schema 与代码注释已同步校准对齐 | — | **已解决**（确立源文件存在时 fail-closed 拒绝注册权威语义） |
| SC-007 | 状态事件命名与写入权威 | wiki/task-lifecycle.md 历史上仅提及 `task_status_changed` | 权威事件为同事务原子落库的 `task_transition`（state_db.py L6820-6833）；`task_status_changed` 为事后 best-effort 轨迹事件（kernel.py L203-222）；wiki 已明确分层与写入权威 | — | **已解决**（明确同事务权威与轨迹旁路分层） |
| SC-008 | “active”集合 | 同一词面 | 路由占用状态集 `ROUTER_OCCUPIED_STATUSES`（`pending`, `dispatched`, `working`, `blocked`, `agent_done`, `rework`，用于 Agent 容量统计）；状态机生命周期活跃态 `ACTIVE_TASK_STATUSES`（含 `paused`、`interrupted`，不含 `pending`，用于执行管线管理）；代码与文档已显式命名解耦 | — | **已解决**（解耦为状态机生命周期活跃态与路由负载占用态） |
| SC-009 | 预占文件名 | docs/product-specs/agent-policy-spec.md L54、docs/operations/troubleshooting-faq.md L55、L58 历史文档写 `router-reservations.json` | herdr/agent_router.py L16 为 `agent-reservations.json`；文档已统一校准并更正 TTL 说明 | — | **已解决**（统一为 agent-reservations.json） |
| SC-010 | 门禁豁免审批 | 设计意图为人工例外通道 | herdr/kernel.py L850-857 仅校验 reason；operator 为自由字符串；无二次审批；控制台写路由无鉴权 | — | 确认 |
| SC-011 | 工作流初始状态 | herdr/transitions.py L89-97 以 pending 为起点 | herdr/projects.py L593 直接写 running；herdr/state_db.py L1617 占位工作流写 pending | — | **已裁决**（ADR-0007 / OEP-0002） |
| SC-012 | 门禁身份与权威 | 文档：门禁为节点级 GateSpec | 结论按任务：tasks.stage_verdict；verdict 文件按 task_id（kernel 写 gate_verdict_path(task_id)）；覆盖（force-pass）按节点 | — | **已裁决**（ADR-0006 / OEP-0002） |
| SC-013 | 任务类型词表 | schema L53 历史版本称 explore/plan/code/test/review/docs | launch --task-type 取值 fix/feat/refactor/docs/test/chore/perf/ci；schema 与文档已同步校准，兼容别名自动映射为 docs/test，业务类型走 business_task_type | — | **已解决**（D4 落地） |
| SC-014 | fix-loop 可作废集合 | controller 注释要求与矩阵“→ superseded”一致 | FIX_LOOP_SUPERSEDEABLE 已补齐 paused 与 interrupted，与 TASK_TRANSITIONS 完全对齐 | — | **已解决**（D5 落地） |
| SC-015 | 门禁豁免守卫 | 控制台 force-pass 带 expected_version（console/control_api.py L409） | CLI 与 factory 的 force-pass 不带 expected_version（bin/herdr-factory L652-656）；bypass 亦不带 | — | 确认（控制台确认提示未核对） |
| SC-016 | resume 目标 | 矩阵：paused 不可达 dispatched、blocked | bin/herdr-task L8000-8010 按 status_history 回退到 dispatched 或 blocked；set 对非法边的实际行为未验证 | — | 未验证 |
| SC-017 | `herdr-task add` 覆盖写 | — | bin/herdr-task L1199-1203 与 state_db.py L1714-1731 的覆盖写可能回退并发状态（子代理取证） | — | 未验证 |
| SC-018 | 越权状态变更拦截 | wiki/task-lifecycle.md L15：“任何越权状态变更将被 CLI 直接拦截” | 8 类状态机外写入路径，见 states/task_lifecycle.yaml 的 out_of_matrix_writers | — | **已裁决**（ADR-0005 / OEP-0002） |
| SC-019 | 控制台 force-review | 控制台写路由用于人工复审 | console/task_api.py L266-290 直接设置 status=agent_done，无守卫、无事件 | — | 确认 |

## 处置建议（属人工决策，不在本轮落地）

1. SC-001、SC-002：以 herdr/transitions.py 为唯一事实源，删除 CLI 兜底副本或由生成器产出；同步 wiki。
2. SC-003 已撤回：无需处置。
3. SC-004、SC-009 已解决：架构文档已补充“SQLite 为权威、JSON 为导出/只读投影”设计原则；预占文件名与 TTL 已统一为 agent-reservations.json。
4. SC-005 已解决：规范定案为 `retry_node` 为标准推荐字段，`retry_target` 为完全等价兼容别名；DAG 校验与 Controller 调度均双向容错解析，消除打回降级隐患。SC-006 已解决：定案为源定义文件存在时快照失败直接拒绝注册（fail-closed），彻底杜绝模板切换并发污染，更新模板 Schema 与代码注释。
5. SC-007 已解决：明确分层定义——`task_transition` 为 SQLite 强事务内原子写入的状态机单一权威事件；`task_status_changed` 为事务提交后的 best-effort 旁路轨迹事件，供 Observer 与重放分析消费。
6. SC-008 已解决：显式解耦定义 `ROUTER_OCCUPIED_STATUSES` 与 `ACTIVE_TASK_STATUSES`，严格区分工位队列容量占用（含 pending，排队占配额；不含 paused/interrupted）与生命周期管线活跃（含 paused/interrupted，不含 pending），禁止同名混用并补齐全覆盖单元测试。
7. SC-010、SC-015、SC-019：把豁免与人工覆盖纳入权限模型，统一守卫，并为控制台写路由增加鉴权（代码变更）。
8. SC-011、SC-012 已由 ADR-0006、ADR-0007 与 OEP-0002 裁决：工作流初始状态定案为 running（pending 为未装配/草稿态）；门禁解耦为 gate_spec 与 gate_verdict，DB 为调度第一事实源，文件为不可变快照。
9. SC-013、SC-014 已解决：任务类型词表统一对齐为 CLI choices；补齐 fix-loop 可作废集合（D4、D5）。
9. SC-018 已由 ADR-0005 与 OEP-0002 裁决：状态机外写入显式收敛为 privileged_transitions 特权流转契约，严禁直写 SQL/save_task 绕过网关。
