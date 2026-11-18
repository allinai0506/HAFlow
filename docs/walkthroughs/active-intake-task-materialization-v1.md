# Active Intake Task Materialization V1 实施报告

基线为 `0263a428fff3869d33c03e797650993f9d8ace53` (PR #424)。本次交付阶段二的任务物化（Task Materialization），将已持久化的外部事件安全、幂等、可追溯地转化为 HAFlow 用户 Inbox 中的待处理任务。外部系统仅产生事实，HAFlow 拥有内部任务生命周期与执行范围授权。本次实现不接入任何外部 Connector，不启动 Agent，Dispatcher 保持隔离。 [架构依据](../architecture/active-intake-v1.md) · [协议](../architecture/active-intake-v1-connector.md) · [对抗式审查](../engineering/reviews/active-intake-task-materialization-v1.md)

## 调用路径与执行边界

| 路径 | 执行与持久化 |
| --- | --- |
| 初始化与门禁迁移 | `initialize_materialization(path)` 经 State DB 独占锁安装 `MATERIALIZATION_DDL`，记录迁移标记 `active_intake_inbox_v1`。向 `intake_task_links` 补充版本、来源连接、代数、工作区等元数据；同时在 SQLite 建立四项只读/草稿门禁触发器（`intake_inbox_draft_only`、`intake_inbox_no_replace`、`intake_link_task_guard`、`intake_inbox_no_delete`），确保物化任务在确认授权前物理阻断派发。 |
| 物化执行 | `services/herdr-intake.py` 沿用 Core 的单一在途领取与租约。调用 `materialize(store, claim, policy)`，在同一个 `synchronous=FULL` 的 SQLite `BEGIN IMMEDIATE` 事务内完成对象查重更新、工作目的链接维护、草稿插入、事件关联与尝试完成。 |
| 外部对象与唯一目的 | 外部对象通过 `(tenant_id, provider, provider_instance_key, object_type, external_id)` 唯一锚定；工作链接按 `(tenant_id, object_id, purpose_key)` 唯一约束。同一外部对象在 `review` 目的下严格单任务，多事件与乱序事件复用同一工作目的。 |
| 单操作员收件投影 | `IntakeInbox` 封装租户与单认证操作员上下文。`list_items()` 联合外部对象、最近触发事件、正本投递回执与现有 `user_inbox_tasks`，多维先过滤后分页。外部快照与来源更新独立展示，不覆盖操作员手动调整。 |
| 操作员操作与审计 | 操作员通过受保护的 `/api/inbox/intake` 执行 `assign`（分配给我）、`unassign`（撤销归属）、`edit`（修改标题/描述/项目/工作区/优先级）、`confirm`（保存确认回执）、`ignore`（忽略事项）及 `reactivate`（重新激活）。所有修改原子持久化并追加审计日志。 |
| 数据库执行门禁 | 确认操作仅在 `intake_task_links` 生成包含内容指纹的 `confirmation_json` 回执，底层 `user_inbox_tasks.status` 保持 `DRAFT`。Dispatcher 仅领取 `PENDING`，直接阻断外部事件对执行引擎的误穿透。 |

[物化源码](../../herdr/intake/materializer.py) · [Inbox 投影](../../herdr/intake/inbox.py) · [控制台接口](../../console/inbox_api.py) · [前端控制器](../../console/static/console/scripts/intake.js)

## 严格遵守 V1 身份边界

- **单租户与单操作员绑定**：V1 实例仅绑定一个租户、一位经过认证的本地操作员与一个用户级 Inbox。服务端配置通过受信任环境变量 `HAFLOW_INTAKE_INBOX_CONFIG` 注入，`bind_instance()` 将租户与操作员写入 `schema_meta.intake_inbox_owner_v1`。任何请求篡改或实例漂移均触发 `ScopeDenied`。
- **外部身份纯属事实记录**：外部事件中的 `assignee`、`creator`、`approver` 等身份字段仅保留在外部快照 `identities` 字典中作为溯源事实，绝不映射或猜测为 HAFlow 内部用户，严禁建立多用户改派机制。
- **分配权限单向收敛**：仅允许将未分配事项“分配给我（Assign to Me）”或“撤销归属（Remove Assignment）”。撤销后事项回到 `PENDING_ASSIGNMENT` 待分配状态。API 严格拒绝任何包含任意 `user_id` 的改派请求。
- **工作区与所有权分离**：Project 与 Workspace 独立于收件人身份。仅当连接策略具备在 `load_projects()` 中唯一且经过验证的映射时才自动填充；否则保持 `NULL` 进入待分配，绝不冒进派发。

## 事务与幂等保证

1. **同库单写事务**：Event 状态跃迁、Attempt 记录关闭、Object 快照更新、Task Link 状态及 `user_inbox_tasks` 草稿插入严格在同一个 SQLite 连接与同一个写事务中执行。
2. **防孤儿与防双写**：任何 SQL 错误或进程意外崩溃（`os._exit`），SQLite 事务自动全量回滚，绝不残留无来源关联的孤儿任务。
3. **并发安全与唯一约束**：两个 Worker 并发竞争同一外部对象时，受 `(tenant_id, object_id, purpose_key)` 唯一约束与 SQLite 写锁保护，必有一方以 `DUPLICATE_OR_STALE_OBJECT` 安全跳过，全局最终仅物化出单一任务。
4. **人工编辑抗覆盖**：操作员手动修改标题、描述、项目或优先级后，后续外部 `update` 事件仅刷新外部对象快照，不覆盖操作员已编辑的任务正文与范围。

## 验收结果

物化核心套件 `tests/test_intake_materialization.py` 共 24 项测试全部通过，覆盖：
- 单次物化与来源保留、非工作事实与无效 Schema 拦截；
- 操作员无派发确认、撤销归属与再次分配；
- 并发竞争领取与重复物化去重；
- SQL 注入故障三点回滚与崩溃自愈；
- Worker 子进程崩溃退出防孤儿；
- 人工编辑与范围映射抗外部覆盖、关闭状态单向固化；
- Console HTTP Token 门禁与旧版 Inbox 隔离；
- 真实 SQLite 写锁争用自愈、凭证轮换正文封存与跨连接关闭；
- 独立 CLI Worker 读取可信实例策略端到端执行。

全量相关集成测试（77 项）与历史 Inbox 回归（144 项）全部通过。

## 实施决策与验证边界

- 外部任务在控制台必须输入受信任凭据（`HAFLOW_INTAKE_INBOX_TOKEN`，至少 32 字符）解锁，凭据仅在前端页面内存中保存，控制台刷新或锁定后立即销毁。
- 未分配事项（`PENDING_ASSIGNMENT`）或未指定有效工作区的任务不允许进入可执行队列；SQLite 触发器从物理存储层拦截任何将 active intake 任务变更为 `PENDING` 的企图。
- 外部对象的 `CLOSED` 终态不可逆。一旦收到关闭事件，关联任务即刻阻断进一步确认，避免对已作废的外部工作进行内部派发。

[并发与故障注入报告](active-intake-task-materialization-v1-faults.md) · [独立对抗审查](../engineering/reviews/active-intake-task-materialization-v1.md)
