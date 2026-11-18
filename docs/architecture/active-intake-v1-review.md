# Active Intake V1 场景验证与对抗审查

[总体架构与数据模型](active-intake-v1.md) · [Connector 接口和交付协议](active-intake-v1-connector.md)

## 场景验证方法

以下是按供应商可核查资料与设计约束推演的验收向量。W1、P1、D1 等为合成标识。除列明的源码测试和 SQLite 反例外，场景结果为设计预期，实际接入及运行证据均待验。

### A. Linear Issue Webhook

适配器实现 `Connector + WebhookConnector + ObjectReader`，完整 Manifest 见 [Linear Manifest](active-intake-v1-connector.md#manifest-字段)。接收时用已注册连接的签名密钥验证原始字节签名，并比对已签正文内的 `organizationId`、注册 webhook 及允许的作用域。 `Linear-Delivery` 是传输标识，`webhookId` 标识订阅。采用由已签 `organizationId/type/action/createdAt/data.id` 和稳定业务字段规范化得到的事实摘要作为事件 ID。排除 `webhookTimestamp` 和订阅 ID。若供应商无法提供可区分完全相同的业务事实的信息，允许合并这些等价观察，保留投递审计。相同 Issue 用稳定 `data.id`，工作目的固定为 `issue-triage`。 [Linear Webhooks](https://linear.app/developers/webhooks)

| 步骤 | 输入 | 设计预期 |
| --- | --- | --- |
| A1 | W1 创建 I1，负责人已映射，但未选项目和工作区。 | 1 个正本 Event、1 个对象、1 个用户 DRAFT。 |
| A2 | W1 正文重投。或者重投身份头改变且正文等价，或已入账的已验签旧副本到达。 | 正本和 DRAFT 均为 1。记录重复投递，完成持久确认后返回 HTTP 200。 |
| A3 | W2、W3 依次修改 I1，随后较旧 W0 到达。两个有授权的订阅都观察 I1。 | 新事实可入账，旧版本不能覆盖较新快照。即使事件域不同，对象和目的唯一键仍只保留一条任务。 |
| A4 | 正本尚未提交时崩溃，或正本提交后 ACK 丢失。 | 首次失败依赖来源重投或补查。已提交的重投只命中正本，消费独立恢复。 |
| A5 | 伪造或不同组织事实，或签名有效但已过期的首见事实。 | 拒绝，无任务。告警缺口，使用经批准的只读补扫；没有供应商历史时不能恢复所有中间更新。 |
| A6 | 用户确认范围，Workflow 完成后外部 Issue 再更新或关闭。 | 授权门槛通过时由既有 Dispatcher 执行。外部更新保留事实，不建立第二条 Task，也不把外部关闭写为 COMPLETED。 |

**供应商待验。** 实际 webhook 作用域和 Issue 正文样本、组织绑定、事件字段稳定性、重投是否刷新已签发送时间及对象读取的权限。Linear 公共端点需要 HTTPS，成功为 HTTP 200，5 秒响应限制，失败最多重试三次。 [Linear Webhooks](https://linear.app/developers/webhooks)

### B. 钉钉 OA 待办与审批

选用企业自建应用的 Stream 事件模式作为候选。适配器实现 `Connector + StreamConnector + ObjectReader`。两种推送接收都进入同一 DeliveryBatch，Stream 不声明 HTTP body 签名。使用绑定企业的认证会话，还须检查事件内企业 ID 与租户、模板范围的对应。供应商确证可用的 `eventId` 作为来源内事件身份，传输 `messageId` 只做 ACK 关联，除非供应商证明它同时是稳定事实 ID。审批任务开始及转交关联审批实例，但对象优先精确到审批工作单元，使用已验证的 `processInstanceId + taskId`；转交后若供应商更换 taskId，须用明确的关联证据复用现有目的，无法证明时停在待处置。审批实例状态单独关联实例对象，仅提供上下文。普通钉钉待办仅在选定企业授予只读数据权限且取得稳定 todoId 时映射为独立的 Todo 对象。已有 OA 任务对应的普通待办镜像须有稳定的原始任务关联才能合流，否则不用于物化。 [审批任务事件](https://open.dingtalk.com/document/development/event-bpms-task-change) · [审批实例事件](https://open.dingtalk.com/document/development/event-bpms-instance-change) · [待办开放能力](https://open.dingtalk.com/document/orgapp/dingtalk-todo-task-overview) · [钉钉 Stream SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk-nodejs/blob/main/README.md)

| Manifest 项 | 规范取值 |
| --- | --- |
| provider / transport | `dingtalk_oa` / `stream`，企业自建应用，认证会话绑定 corp，ACK profile 为持久提交后确认。 |
| 配置 / 凭证 | 允许的 corp、审批模板、可读用户范围和读操作能力。应用密钥及访问凭证使用 `auth_ref`。Stream 身份认证、订阅权限和审批详情/待办读权限分别核验。 |
| events / object_read | `bpms_task_change` → `com.haflow.intake.dingtalk.approval-task.changed.v1`，`bpms_instance_change` → `com.haflow.intake.dingtalk.approval-instance.changed.v1`，均采用独立 data schema。支持读取已授权审批详情。普通待办若启用只读同步，使用同一 PollingConnector 形成 `com.haflow.intake.dingtalk.todo.observed.v1`，不冒用审批事件名。 |
| health | 认证、会话在线、企业及模板绑定、应用可见范围、最近正确投递、持续确认失败。 |

| 步骤 | 输入 | 设计预期 |
| --- | --- | --- |
| B1 | 企业 A 向审批人 U1 派待办，企业 B 使用相同外部用户 ID。 | 仅 A 的企业映射生效。已知用户得到 DRAFT；B 丢失或不匹配时，拒绝跨租户绑定。 |
| B2 | 一条审批人映射不存在或应用看不全负责人。 | 持久保留审批任务对象及未物化链接。管理员核实后原子物化。 |
| B3 | 转交、终止或同一实例不同审批人的并行工作，随后收到陈旧状态。 | 不以实例 ID 把不同工作单元合并，不因账户变化再建已有工作目的。外部权限减少时阻断旧用户读取及新确认。版本、转交对应证据不足时等待处置。 |
| B4 | Stream 正本提交前断开，或提交后 ACK 丢失。 | 前者返回可重投状态或允许供应商重投，后者按事实键确认。未证明重投策略时通过已授权清单对账，不承诺补回不可见事实。 |
| B5 | 用户确认只读审批建议，Workflow 完成，钉钉仍待审批。 | HAFlow 产出建议和依据，钉钉业务状态保持。需要具备原系统权限的人在那里执行正式审批。 |

**供应商待验。** 企业应用对两类事件的真实订阅授权、actor 与受让人字段或详情查询、稳定事件/工作单元标识、传输重投与 SDK 的 ACK 控制点、普通待办关联及只读权限。SDK 的事件确认与交互回调确认要分别核实，不能把机器人例子的自动 ACK 应用于账本。

### C. 金蝶云星空采购订单异常

适配器实现 `Connector + PollingConnector`，可选 `ObjectReader`。资料显示金蝶云星空 V6.0 的 `ExecuteBillQuery` 支持采购订单 `PUR_PurchaseOrder` 和筛选、排序、分页参数。候选增量查询需要目标账套验证 `(修改时间,FID)` 后继谓词与稳定排序，以窗口上界及最后元组继续，每次从首行取一个限量页。若返回分录而非订单头，先证明头查询或用稳定行标识聚合并形成同一对象，禁止跨页漏单。采购异常策略示例为交付期限及剩余量的非零组合，具体规则由业务审批。仅按修改时间的增量流不能发现未修改订单跨过交付期限。若承诺这类异常，另用同一 Polling contract 在受管范围扫描到期未交订单，按稳定扫描周期和快照形成观察，仍复用同一对象目的。异常任务按受管 purpose `delivery-exception-review` 保持一个任务。读取时只取最小判断字段，命中后透出已批准的晚交货和缺量摘要。 [金蝶云星空 WebAPI 接口说明书 V6.0](https://vip.kingdee.com/article/490771221039228672)

| Manifest 项 | 规范取值 |
| --- | --- |
| provider / transport | `kingdee_k3cloud` / `polling`，流为 `purchase-orders`，游标为带版本的固定窗口和 keyset 位置。 |
| 配置 / 凭证 | 批准的服务地址、账套、组织、时区、窗口、页上限和重叠量。授权应用及专用只读账号使用 `auth_ref`。 |
| events / object_read | 观察事件 `com.haflow.intake.kingdee.purchase-order.observed.v1`，最小数据 schema，稳定事件键基于账套、订单 ID、来源版本（可空）及稳定快照摘要。异常判断属于 Intake policy，Connector 只提供读取。 |
| health | 账套匹配、读授权、上次完成窗口、延迟、重试、无进展和供应商数据质量。 |

| 步骤 | 输入 | 设计预期 |
| --- | --- | --- |
| C1 | 从固定窗口拉同一时间戳的 PO-101、PO-102，PO-101 异常。 | FID 决定同时间戳顺序。正常单可只记录事实，异常单在已知用户下生成唯一草稿。 |
| C2 | 故障发生在第二页写事务提交前。 | 页与游标一起回滚。恢复从第一页末尾继续，最终不遗漏第二页。 |
| C3 | 页已提交后进程退出，随后重叠补扫看到 PO-101。 | 续扫从已提交位置开始。重复观察只命中事件或同一个对象目的，草稿仍为 1。 |
| C4 | 修改时间缺失、乱序、供应商返回半页后超时，或过期 worker 回来提交。 | 拒绝推进相应页，保留重试与错误。有效页的旧进程提交由代数和游标 CAS 拒绝。 |
| C5 | 稳定版本无法区分临界瞬态，或者现场只能在可变集合用 OFFSET。 | 对账能覆盖授权窗口内仍可见的事实。只有稳定快照或有界全量能证明完整性时才上线。 |
| C6 | 用户完成 HAFlow 异常分析，采购订单在 ERP 仍未审核或关闭。 | 订单状态继续由 ERP 管理，内部完成只改变 HAFlow 任务状态。 |

**供应商待验。** 目标版本的头表键及过滤、修改时间精度与可见延迟、读取权限、上界形成、排序、页长、删除/状态变化语义和客户网络。只读轮询可以观测当前状态，缺少 CDC 或 tombstone 时无法重建所有历史变更。

## 独立对抗审查

执行者撰写设计。检查者独立使用 CodeGraph 和源码、供应商资料与临时 SQLite 做反例，不修改仓库。评判者使用成稿和反例重新核对设计承诺及发布条件。设计裁决只覆盖已明确的不变量及待验边界。

| 审查议题 | 检查者反例与证据 | 执行者纳入的裁决条件 |
| --- | --- | --- |
| CloudEvents 和重放 | 规范修订号被填为 specversion，轮询凭证被当签名，未签 delivery header 变更可绕过单键去重。 | 固定 1.0、区分签名和会话、稳定业务指纹及已知旧副本规则。 |
| 事件与任务混淆 | 多事件、不同订阅或 owner 更换均可能写多条任务。实际 Schema 的同一幂等键写给两用户可产生两行。 | Event 唯一键与对象目的唯一键落库，链接早于物化。待分配和 terminal 墓碑持久化。 |
| 数据丢失 | Store 自行提交，账本有正本也可能未物化；默认 WAL NORMAL 对掉电不保证已 ACK 行持久。 | 同库同连接的 Store 原语、短事务、FULL 写入、确认时限及故障注入。 |
| 用户与企业隔离 | 当前环境身份及 root/admin 旁路不是企业请求身份。来源同 ID 和读路径旁路可串租户。 | 首发物理隔离，可信单操作者，来源绑定和读权限。共享多用户仍是一项新门槛。 |
| 游标跳跃 | 模拟页 1 取 A、B 后集合删除 A，页 2 OFFSET=2 只得 D，漏 C；复合 keyset 返回 C、D。 | 只有证明稳定排序、迟到界限与当前读取边界后推进。外部删除/短暂更新不能从单次观察补全。 |
| 外部状态覆盖 | OA 转交、关闭或 ERP 状态变化不能证明内部已停或已完成。 | 外部快照独立；正文可见性、待处理权限和运行审计分别管理。 |
| 绕过 Dispatcher | `claim_next_task` 只看 workspace/PENDING；当前权限、项目 JSON 和租约之间无统一授权事务。另有直接 Workflow 入口。 | 静态 Connector 不接执行端口，DRAFT-only 先行，执行前证明确认、领取、授权、启动的一致性。 |
| 过度设计 | 再建执行队列、状态表或引入 broker 不能修复本地双写。 | SQLite 账本和现有 Inbox、Dispatcher，独立且有界的 I/O worker。 |

检查者在基线 `5fb037da` 执行 `test_draft_never_dispatched_until_confirmed`、`test_auto_confirm_source_validation_and_user_isolation`、`test_idempotent_task_submission`，结果 **3 passed**。这些用例使用临时状态和替代 runner，覆盖现有行为。两个反例使用内存 SQLite 和可变集合，分别验证用户级唯一键无法保证跨 owner 单任务，以及 OFFSET 漏页。启动授权、真实 Agent、供应商轨迹及 V1 的实现均须后续实证。

### 评判者裁决

有条件接受同库账本、稳定目的键和原子草稿物化作为设计候选。主动任务执行和企业共享实例均为 **BLOCKED**，等待各自门槛的实施证据。评判者用临时 SQLite 和现有 Inbox API 创建 `source_type=webhook` 的 DRAFT，再由同一 owner 调普通确认 API，实际转换为 PENDING。实验替代了范围和主体环境以及 sweep，保留实际 Store。它证明只隐藏按钮或依赖创建时的 auto_confirm 条件会失效。原有注册栅栏可用作候选接入点，现状没有授权串行保证。

评审后补入五项约束。

1. 第一条主动草稿前部署可信 link 识别、服务端 DRAFT-only 门闸和旧版回滚隔离。
2. 授权撤销、配置换绑与实际启动使用单次授权的原子判定。已阻塞的 PENDING 不占住队头。
3. 未领取 DRAFT 的受控改派保持同一任务身份，授权撤销后立即隔离旧权限。
4. ACK 前保存规范信封及最小 data。畸形事实的原始信封重放后保持不变。
5. 仅在坏行可定位、可排序、可恢复时允许持久隔离后推进轮询。

检查阶段无连接调用或生产状态变更。租户隔离、权限撤销竞态、掉电恢复和三家真实接入仍为 UNVERIFIED。

## 验收交叉索引

| 用户验收项 | 设计证据 | 尚待实施验证 |
| --- | --- | --- |
| 1 兼容人工 Inbox | 原状态、队列、API 契约和现有测试 | 老库迁移及完整人工链路回归 |
| 2 三来源同模型、3 同 Event Inbox | 三组同契约推演及传输事务契约 | 各供应商沙箱、实际样本、真实错误 |
| 4 重复事件、5 正确对象关联 | 两层唯一键、并发事务和反例 | 创建/更新/重归属/重放并发故障注入 |
| 6 待分配、7 不进入 Dispatcher | 待分配 link、DRAFT 与授权门槛 | 撤权、换绑、重试、栅栏、直接入口 |
| 8 生命周期独立 | ER 与事件/任务转换、Workflow 对账 | 运行反例和外部状态变更 |
| 9 重试审计重放 | 栅栏、重试、DLQ 与过期策略 | 崩溃、掉电、ACK 丢失和权限复验 |
| 10 无新消息中间件 | SQLite 同库决策 | 吞吐、留存和写锁 SLO |
