# Active Intake Core V1 故障注入报告

所有测试均在隔离临时 SQLite 文件执行。子进程故障用 `os._exit(37)` 注入。消费租约与退避使用可控 UTC 时钟推进，锁竞争使用真实 SQLite 写锁。 [测试源码](../../tests/test_intake_faults.py)

| 条件 | 证据 |
| --- | --- |
| 迁移最后一个 DDL 执行失败 | 全部 Intake DDL 与迁移标记回滚。随后可重新迁移且外键检查为空。 |
| 投递回执写入失败 | SQLite 触发器中止 INSERT。正本与回执均为空，重试得到 ACCEPTED。 |
| 第二连接持有写锁 | Dedicated connection 返回 SQLITE_BUSY，实测受限于已配置的 250 毫秒级等待，事务无正本、无回执。 |
| COMMIT 前退出 | 子进程在执行 COMMIT 前直接退出，重开库观察到 0 正本、0 回执。 |
| COMMIT 后 ACK 丢失 | 子进程提交后退出。重投返回 DUPLICATE，账本有 1 个正本、2 条独立回执。 |
| 消费过程中退出或重试过程中再次退出 | 已提交的 PROCESSING 和 Attempt 可读。重启后回收旧租约，关闭旧 Attempt，重试到期后可再次领取。 |
| 旧 Worker 和游标租约继续写 | 过期、代数变化或缺少随机所有权凭证均会拒绝结果。 |
| 游标页提交前或提交后退出 | 前者保留上一页位置且没有半页正本或回执。后者重启后读到完整新水位和新页。 |
| 坏行、乱序、跨流页 | 无游标推进和部分正本。结构上可归属的坏页逐条生成拒绝回执，混用异流请求在提交前拒绝。 |

固定种子可靠性验收通过 25 个窗口页接收 1,000 条事件，重投其中 200 条，在消费中随机注入可重试故障和真正的子进程退出。断言每个中间位置与最后一行相同，终值水位为 1,000，账本键完整，回执为 1,000 ACCEPTED 和 200 DUPLICATE。每个正本恰有一条 PROCESSED 尝试，Workflow 与 Task 表均为空。

复现命令

```bash
python3 -m pytest -q tests/test_intake_store.py tests/test_intake_cursor.py tests/test_intake_worker.py tests/test_intake_faults.py
```

断电、文件系统异常及供应商实际重投和分页完整性为 UNVERIFIED。
