# Active Intake Core V1 独立对抗审查

执行者实现并修复。检查者只读审查，使用临时 SQLite 复现候选缺陷并复查。评判者独立运行进程崩溃、回滚、旧 Inbox 回归和 1,000 条合成事件压力样本。检查者复审未发现残留 P0 或 P1，评判者独立执行 148 项专项和兼容测试，未复现残留 P0、P1 或 P2。 [故障注入报告](../../walkthroughs/active-intake-core-v1-faults.md)

| 级别 | 问题与位置 | 复现条件与实际影响 | 修复及回归 |
| --- | --- | --- | --- |
| P1 | 投递验证可改绑，`protocol.py Delivery`、`store.py accept_event` | 两租户配置相同的供应商来源。把 A 的验证结果传给 B，或连接轮换后把旧证据标成新代数，候选曾接受。 | 验证结果绑定可信作用域和配置代数。 `test_verified_delivery_cannot_cross_scope_or_revision` |
| P1 | Polling 页可跨流提交，`cursor.py commit_page` | 同连接两个流使用同一时间窗，A 的页带入 B 的租约，候选曾推进 B 的水位。 | 固定流能力，页携带原请求并匹配租约、位置与代数。 `test_foreign_stream_response_and_source_do_not_move_watermark` |
| P1 | 通用事件 API 暴露 Intake 操作审计，`herdr/state/events.py list_events / record_event_if_absent` | 通用列表显式查保留来源，或带相同事件类型及 actor 调用幂等写入，可读到异租户审计。 | 通用入口过滤保留来源，Intake 审计另经授权及作用域查询。 `test_admin_audit_is_scoped_and_private` |
| P2 | 坏页丢失已归属投递的审计，`cursor.py commit_page` | 页内一条校验失败，全部正本正确回滚，但候选只保留一条拒绝回执。 | 保存点回滚整页正本后，每条可归属投递独立记拒绝回执。 `test_bad_row_does_not_advance_or_commit_partial_page`、`test_structurally_attributable_bad_position_receipts_without_advancing` |
| P2 | 到期重试饥饿，`store.py claim_next_event` | SQLite 按 NULL 重试时间先取新事件，持续进件时旧重试得不到处理，写事务内还有临时排序。 | 两个有界索引头按可领取时间选择，检查者用 EXPLAIN 确认无临时排序。 `test_due_retry_does_not_starve_behind_newer_work` |
| P2 | 正常元数据能拼装有效租约，`store.py _processing`、`cursor.py _validate_lease` | 持有同范围读权限的本地代码可尝试用已读代数和到期值构造所有权。 | 所有权随机凭证独立于可见元数据，结果提交核验持久化摘要。 `test_claim_requires_private_capability`、`test_cursor_position_metadata_does_not_grant_ownership` |
| P2 | 事实时间与 JCS 输入边界，`protocol.py` | 畸形 UTC 偏移被规范化，不同写法的同一时间触发冲突，超长指数可产生未分类异常，超安全精度的数字可能无声合并。 | 先验证偏移、保留准确 UTC 时间、拒绝不可表达数字与 I-JSON 非法字符，验证规范数值往返。 `test_exact_time_and_ijson_numbers_at_ingress` |
| P2 | State DB 路径缓存与策略代数不匹配，`schema.py initialize`、`store.py Policy` | 删除隔离库并在同进程重新初始化同一路径，路径缓存曾跳过初始化。自定义代数写入后默认服务无法领取。 | 显式初始化复用重建缓存入口，V1 策略代数固定。 `test_reinitialize_replaced_database_at_same_path`、`test_worker_service_consumes_and_stops_without_dispatch` |

评判者另以种子 421 运行临时库，观察到 1,000 个正本全部成功处置，200 次重复投递、183 次临时失败和 38 次领取后子进程退出，最终水位为 1,000。结论覆盖受信本地 Intake Core。真实供应商证据、掉电恢复、需加密留存的原文路径和企业共享实例继续按既定门槛验收。

[审查对象源码](../../../herdr/intake/) · [验收测试](../../../tests/test_intake_faults.py)
