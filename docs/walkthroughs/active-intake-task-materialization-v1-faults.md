# Active Intake Task Materialization V1 并发与故障注入报告

所有测试均在隔离临时 SQLite 文件（WAL 模式、`synchronous=FULL`）中执行。通过 SQLite 触发器注入 SQL 执行异常，通过 `os._exit(74)` 注入子进程崩溃，通过 `Barrier` 与多线程执行器模拟真实 Worker 并发争用，通过前置写锁模拟真实数据库锁争用。 [测试源码](../../tests/test_intake_materialization.py)

## 故障与并发场景矩阵

| 场景分类 | 注入条件 | 观察证据与系统行为 | 验收结论 |
| --- | --- | --- | --- |
| 事务原子性 | 在 `user_inbox_tasks` 插入点通过触发器抛出 `IntegrityError` | 事务全量回滚，`user_inbox_tasks`、`intake_task_links`、`intake_objects` 表记录数严格为 0，外键检查通过；租约过期后接替 Worker 成功恢复并完成物化。 | PASS |
| 事务原子性 | 在 `intake_task_links` 更新 `task_id` 点注入异常 | 事务全量回滚，无残留孤儿任务与空悬链接；重试后正常完成物化。 | PASS |
| 事务原子性 | 在 `intake_events` 更新 `status = 'PROCESSED'` 点注入异常 | 事务全量回滚，事件维持 `PROCESSING`，尝试保持未完状态；过期后重新被接替 Worker 领取并成功完成。 | PASS |
| 进程崩溃 | 子进程在执行 `insert_task` 之前立即执行 `os._exit(74)` 强退 | 数据库连接非正常断开，SQLite 自动回滚事务，各表均无半成品数据；恢复后重新领取并成功物化。 | PASS |
| 进程崩溃 | 子进程在执行 `insert_task` 之后但在事务提交前立即执行 `os._exit(74)` 强退 | SQLite 写事务未提交即终止，底层草稿任务被原子回滚，无孤儿 Task 产生；超时回收后重新物化成功。 | PASS |
| 并发竞争 | 两个 Worker 线程通过 `Barrier` 门闩并发为同一外部对象的两条不同事件执行物化 | 数据库唯一约束 `(tenant_id, object_id, purpose_key)` 与写锁协同生效：一方成功获得 `PROCESSED`，另一方以 `IGNORED`（`DUPLICATE_OR_STALE_OBJECT`）安全跳过，全局严格只创建 1 个 Task。 | PASS |
| 锁争用 | 专用连接持有 `BEGIN IMMEDIATE` 锁，Worker 尝试执行物化 | Worker 捕获 `sqlite3.OperationalError` (`SQLITE_BUSY`)，事件安全退避并保留尝试；锁释放且租约到期后成功自愈。 | PASS |
| 租约过期 | Worker 在租约已过期（`now >= lease_until`）的情况下尝试提交物化结果 | 触发 `LeaseLost` 异常，写事务立即回滚，防止过期 Worker 越权覆盖新接替者的结果。 | PASS |
| 重复事件 | 同一事件通过 `accept_event` 重复入账两次，随后触发 Worker 消费 | 第二次入账返回 `DUPLICATE` 回执，物化 Worker 仅处理一次，事件保持 `PROCESSED`，任务严格唯一。 | PASS |
| 乱序事件 | 先收到 version=2 的更新事件，随后收到迟到的 version=1 工作创建事件 | 系统正常处理更新事件并建立工作链接；迟到的旧事件被识别为陈旧版本，安全忽略，任务标题与正文保持最新。 | PASS |
| 冲突版本 | 外部系统推送相同 version 但不同快照指纹的事实 | 外部对象状态被标记为 `CONFLICT`，事件标记 `IGNORED` (`OBJECT_VERSION_CONFLICT`)，关联任务的确认操作被物理阻断。 | PASS |
| 外部关闭 | 外部事项收到 `action = 'closed'` 事件 | 外部对象状态转为 `CLOSED`，清空确认状态，操作员无法再确认该事项，杜绝将已失效的外部工作推向执行。 | PASS |
| 执行防穿透 | 外部通过直接 SQL 或 API 尝试将 active intake 关联任务的 status 更新为 `PENDING` | 触发器 `intake_inbox_draft_only` 立即执行 `RAISE(ABORT)` 中止更新，保证 active intake 任务物理上不可被 Dispatcher 领取。 | PASS |
| 保留期门禁 (P2-01) | 数据库中存在未及时被 prune 清理、但接收时间已超过 `retention_ms` 的事件 | 物化事务检测 `received_at <= now - retention_ms`，直接更新事件为 `IGNORED / NO_RETAINED_CANONICAL_DATA`，绝不物化新任务。 | PASS |
| 终态清理 (P2-02) | 外部事项处于 `SOURCE_CLOSED` 或 `SOURCE_CONFLICT` 状态 | 允许操作员通过 `unassign`（撤销归属）与 `ignore`（忽略事项）清理控制台视图，依然严格阻断 `edit` 与 `confirm`。 | PASS |
| 执行归属 (P2-03) | 外部事项完成物化但未配置或映射有效 Project/Workspace | 任务虽然归属操作员，但投影与视图维持 `PENDING_ASSIGNMENT`；确认操作抛出 `StateConflict` 阻断，仅在显式编辑补齐有效范围后方可转 DRAFT 并确认。 | PASS |

## 复现命令

```bash
# 运行全部物化与故障注入测试套件（27 passed）
python3 -m pytest -q tests/test_intake_materialization.py
```

断电掉电、物理磁盘故障与跨网络磁盘（NFS）挂载场景未在本报告范围内测试（标记为 UNVERIFIED）。本地可靠文件系统与单机 SQLite WAL 约束已得到证明。
