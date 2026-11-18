# Active Intake Task Materialization V1 验收与对抗审查

[实施报告](../../walkthroughs/active-intake-task-materialization-v1.md) · [故障注入报告](../../walkthroughs/active-intake-task-materialization-v1-faults.md) · [架构依据](../../architecture/active-intake-v1.md)

## 范围与实际入口

一个 HAFlow 实例绑定一个租户和一个经认证的 Inbox 收件人。显式启用的连接默认自动把符合工作策略的事项交给该操作员。连接策略要求手动审查时，事项显示为待分配。外部负责人、创建者和审批者 ID 仅保留为来源事实。人工可核对并分配给自己、撤销归属、更新任务内容和范围、忽略事项或手动重新激活已忽略的开放事项。

由受信装配接收的事件进入 `EventStore.accept_event`。独立 `services/herdr-intake.py` 沿用领取、租约、重试和尝试记录。物化在同一个 FULL SQLite 写事务内提交对象、唯一工作目的链接、现有 Inbox 草稿、来源引用和结果审计。Console 的同一 Inbox 通过带实例凭据的读取路径显示外部草稿及待分配链接。确认回执使草稿显示为已确认，数据库任务仍是 `DRAFT`。SQLite 通过可信链接阻止旧入口把它推进到执行状态。

## 试点配置

在独立租户的 worker 和私有 Console 进程提供同一份受信环境配置：

```json
{
  "tenant_id": "tenant",
  "user_id": "operator",
  "connections": {
    "connection-a": {"project_id": "project-a", "workspace_id": "workspace-a"},
    "connection-b": {"auto_assign": false}
  }
}
```

将 JSON 放入 `HAFLOW_INTAKE_INBOX_CONFIG`。Console 使用单独的 `HAFLOW_INTAKE_INBOX_TOKEN`，由部署环境提供至少 32 字符的随机凭据。打开现有 Inbox 后用该凭据解锁外部任务，凭据只在页面内存保留。连接映射只接受注册表中的唯一项目及其关联工作区。配置或验证失败时范围留空。稳定租户和收件人绑定写入实例数据库。

使用受信本地装配注册并验证连接，再运行 `python3 services/herdr-intake.py --tenant tenant --connection connection-a`。规范工作事实使用 `com.haflow.intake.work-item.observed.v1` 以及 `urn:haflow:intake:work-item:v1`，数据包含稳定 `object_type`、`external_id`、可比较正整数 `version`、`action`、`title` 和 `description`。可选 `identities` 仅接受 `assignee`、`creator`、`approver` 的外部标识。工作事实使用 `work`，单纯更新使用 `update`，关闭使用 `closed`，无工作价值的元数据使用 `metadata`。生产者负责内容白名单、数据最小化与已授权来源校验。

## 对抗式审查

| 攻击面 | 反对者发现及修复 | 裁决所依据的场景 |
| --- | --- | --- |
| 重复、并发与失败 | Event 租约与数据库对象目的唯一键在同一写事务约束结果。任务创建、链接更新、事件结果和进程退出均可回滚。 | 并发不同事件、重复领取、三点 SQL 错误、两点进程退出、SQLite 写锁超时及恢复 |
| 缺策略与输入 | 缺策略曾会遗失可恢复工作事实。现进入既有重试或死信。严格工作事项 schema 在物化前校验。 | 补策略后重试、非法 schema、metadata、关闭先到 |
| 用户和租户 | 请求凭据绑定实例配置，数据读取校验租户、当前连接与收件人。外部身份不参与收件决策。旧 Inbox 入口按可信 link 过滤。Console 刷新后移除已撤权的卡片与详情。 | HTTP 未认证、请求传 user_id、跨租户、原列表与详情隔离 |
| 授权变更与跨连接 | 独立检查发现旧授权正文可能重新可见，或来源连接可能被错记。读取分别核对初次工作、任务正文及最新快照的连接授权和代数。 | 撤权后重新启用、两个授权连接共用事项、正文来源变更后被撤权 |
| 外部更新和终态 | 保留操作员内容和手动归属。乱序关闭使待办不可确认。同版本冲突阻断确认。忽略和手动重新激活保留同一任务 ID。 | 手动编辑再更新、撤销后工作事件、关闭和版本冲突、忽略与恢复 |
| 确认与执行 | 普通确认和通用写入口受数据库门闸约束。Active Intake 确认保存本地回执，Dispatcher 只领取其原有队列。 | 真 Store、真 Dispatcher claim、真实 Console 浏览器的分配、编辑、确认和撤销 |
| 来源与分页 | 查询绑定最近触发处置的事件及其正本回执，来源快照独立追溯，后端先筛项目和优先级，再分页。 | 浏览器来源详情、HTTP 读取、分页边界及旧数据库升级 |

## 发布验证边界

- 授权代数变更后，旧正文仍保留审计但对 Inbox 隐藏。本阶段采用保守封存，不提供跨授权代数重新发布旧正文的入口。新事项可用新授权入箱。
- Fact 与 receipt 继续使用 Core 的保留期。到期后任务仍可定位事件元数据和连接，UI 将已清理的投递回执显示为未保留。
- 现有 `PROCESSED` 正本保持终态。新规范工作事实应在所有读写进程升级后接入。回滚前隔离主动草稿和旧版读取路径。
- 实测只覆盖临时 SQLite、合成事实和私有 Console。无外部供应商、生产数据、支付模型、Agent 或 Runtime 参与。
