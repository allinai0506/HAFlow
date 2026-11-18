# Active Intake Core V1 实施报告

基线为 `44b1b2edfe59ab7d937c6b07b77bb930f80796d7`。本次交付阶段一的事件持久化、消费和轮询游标。输入端是仅供可信本地装配调用的 Python API。对外接入、外部对象关联和 Inbox 任务物化仍按原设计的阶段顺序执行。 [架构依据](../architecture/active-intake-v1.md) · [协议](../architecture/active-intake-v1-connector.md)

## 调用路径

| 路径 | 执行与持久化 |
| --- | --- |
| 初始化 | `initialize()` 复用 State DB 路径、初始化锁和事务化 DDL。在同一个文件使用现有 `schema_meta` 记录 V1 迁移。新建和旧库升级均由 Intake 显式初始化触发。 |
| 投递 | 可信调用方在 SQLite 之外完成验证，向 `accept_event()` 提供绑定租户、连接和配置代数的 `Delivery`。内核校验 CloudEvents 1.0、来源绑定和有界 I-JSON，按租户与 `source/id` 去重。正本或去重判定与独立回执同事务提交，提交成功后才返回回执。 |
| 消费 | 独立服务 `services/herdr-intake.py` 启动一个在途领取，原子创建 Attempt、增加代数并设置租约。Worker 在写事务之外读取并处理 canonical data，处理事务核验作用域、授权代数、租约与随机所有权凭证，再提交处置与 Attempt。 |
| 失败与重放 | 每轮最多 6 次尝试。临时错误使用有界指数退避与 jitter，并接受毫秒级 Retry-After；永久错误或预算耗尽进入死信。重放经本地可信授权、死信代数和数据可用性检查后重新入列，审计保留操作者、原因和原身份。 |
| Polling | `CursorStore` 绑定租户、连接、流、来源能力及授权代数。页响应携带原领取请求，续页位置取已排序记录的最后一个 UTC 毫秒与唯一键。页内正本、回执及推进一起提交。已知归属的坏页保留拒绝回执，按原位置重试。 |
| 审计读取 | 投递与尝试分别走 `list_receipts()` 和 `list_attempts()`。操作审计通过现有 State DB events 事务写入，专用 `list_admin_audit()` 校验 actor 并限定租户与连接。通用事件读取和幂等接口避开这个保留来源。 |

[Intake 源码](../../herdr/intake/) · [服务入口](../../services/herdr-intake.py)

## 验收结果

专项和兼容回归共 148 项通过。测试覆盖 Intake 新旧数据库与故障注入、Inbox schema/store/Console/Dispatcher/E2E，以及 State DB v2 与既有 DDL 顺序。评判者在同一变更集独立复跑通过。

## 实施决策与验证边界

- 普通 State DB 继续使用现有配置。Intake 使用 WAL、`synchronous=FULL`、外键和 250 毫秒 busy timeout。服务仅由本地显式启动，每进程一个在途领取，支持 SIGINT 和 SIGTERM。SQLite 仍是同文件单写者。
- 本阶段原始字节只计算 SHA-256 摘要。不存在原文读取端口。canonical data 有大小、深度和 90 天默认限期，清理保留幂等键。需要加密留存原文才可恢复的坏行先拒绝推进。对象与任务链接表提供关系约束，保持空置。
- Keyset 实现使用 `(UTC 毫秒, 稳定键)` 和固定窗口。可信调用方须证明数据按此顺序读取、明确耗尽且无缺行。实际供应商的排序、时间精度、补扫和耗尽承诺仍待对应接入验收。
- 目录同步、可靠文件系统和硬件掉电是部署条件。进程退出测试证明 SQLite 事务在软件故障下的行为，掉电恢复仍为 UNVERIFIED。

[故障注入报告](active-intake-core-v1-faults.md) · [独立对抗审查](../engineering/reviews/active-intake-core-v1.md)
