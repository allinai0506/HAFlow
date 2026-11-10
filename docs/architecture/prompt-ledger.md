# Prompt Ledger V1

Prompt Ledger 记录 HAFlow 交给 Agent 传输通道的最终文本，以及这份文本后来走到了哪一步。它不组装新的 Prompt，也不判断模型是否理解了指令。

## 数据存放

快照写在现有状态库的 `prompt_snapshots` 表。`herdr/state/schema.py` 在首次连接时执行 `CREATE TABLE IF NOT EXISTS` 和不可变触发器，没有单独的迁移脚本。状态变化另写 `events`，事件类型是 `prompt_ledger_transition`，来源是 `prompt-ledger`。事件载荷只含 `prompt_id`、状态和证据级别，不含 Prompt 正文。

回执路径原有的 `*_dispatch_prepared` 事件仍保存一份 Prompt，供中断后重发。那份载荷可以被后续修复改写，所以它不是审计原件。Ledger 是不可变副本。V1 接受这两处各存一份正文。

## 快照身份

每次新的投递生成新的 `prompt_id`（`ps_` 加随机十六进制）。同一 `task_id + run_id + intervention_id` 只能有一行。相同正文再次记录时返回原行；正文变了就拒绝覆盖。新的返工使用新的 intervention id，因此保留上一轮。

`prompt_sha256` 是最终投递字符串的 UTF-8（`surrogatepass`）SHA-256。脱敏摘录、截断屏幕和转换后的文本都不是这个哈希的输入。单份正文上限是 4_000_000 字节；超过上限时拒绝建快照，不保存截断副本。

## 状态

合法迁移：

- `GENERATED` → `DISPATCH_PENDING`
- `DISPATCH_PENDING` → `DISPATCHED`、`DELIVERY_FAILED`、`DELIVERY_UNVERIFIED`
- `DISPATCHED` → `DELIVERY_VERIFIED`、`DELIVERY_FAILED`、`DELIVERY_UNVERIFIED`
- `DELIVERY_UNVERIFIED` → `DELIVERY_VERIFIED`、`DELIVERY_FAILED`

`DELIVERY_VERIFIED` 和 `DELIVERY_FAILED` 是终态。`apply_outcome` 会补齐中间状态，只有最后一步写入真实证据。触发器同样拒绝跳过中间状态，并在 `dispatched_at` 一旦写入后禁止改它。

若发送已经返回、但快照仍停在 `GENERATED` 或 `DISPATCH_PENDING`，下一次发现 `*_dispatched` 事件时只把快照补到 `DISPATCHED`，不重发，也不升到 `DELIVERY_VERIFIED`。

证据级别：

| 状态 | 含义 |
| --- | --- |
| `GENERATED` | 最终文本已入库 |
| `DISPATCH_PENDING` | 传输即将开始，`dispatched_at` 在这一步写入 |
| `DISPATCHED` | 传输命令返回 0。级别 `transport_command_accepted`。终端通道仍是 `NOT_CONFIRMED` |
| `DELIVERY_VERIFIED` | 未截断的终端缓冲包含同一份完整文本。级别 `tty_channel_accepted` |
| `DELIVERY_FAILED` | 输入未被接受：`agent_blocked`、`E2BIG`，或遗留路径的 send-text 被拒绝 |
| `DELIVERY_UNVERIFIED` | 传输已经开始，但无法证明文本是否完整进入通道。超时和启动后的其他异常走这里 |

所有证据都带 `model_receipt: UNOBSERVABLE`。`DELIVERY_VERIFIED` 只证明终端输入通道接受了全文。

`interactive_ready is False`，或 Agent 状态是 `launch_pending` / `starting` 时，已验证结果降为 `DISPATCHED`。

## 接入点

回执投递（`INITIAL`、`REWORK`、`RETRY`、`VERIFY`、`RENEW`）在 `herdr/supervisor_delivery.py` 的 `deliver()` 旁路记录。Ledger 失败使用 SAVEPOINT 回滚自己的写入，并打印 `[PROMPT LEDGER]`，不改变原来的投递结果。`transport_started` 已记录而快照仍停在生成或待投递时，恢复路径标成 `DELIVERY_UNVERIFIED`，然后继续抛出 `DeliveryUnknown`，不重发。

无回执的任务派发尾部在 `herdr/task_cli_execution.py` 建快照。intervention id 形如 `legacy:{task_id}:{uuid}`。任务状态和退出码保持原样。无回执的 `herdr-task rework` 同样在发送前建快照，发送结果写入同一行，退出码仍是原来的 2。续期和交付修复的回执发送改用同一套传输分类。

`herdr agent prompt` 返回 0 只表示命令被接受。超时变成返回码 124 时记为 `DELIVERY_UNVERIFIED`。`OSError` 的 `E2BIG` 记为 `DELIVERY_FAILED`，级别 `argv_rejected`，`channel_bytes` 为 `NONE`。本版不在回执热路径上额外调用 `herdr agent get` 或 `pane read`。

遗留派发使用的 `herdr pane read` 文本输出不能证明缓冲未截断，所以这条路径默认不升到 `DELIVERY_VERIFIED`。只有调用方明确给出未截断缓冲时，分类函数才允许升级。主命令超时之后，若 send-text 回退被拒绝并且任务因此被标为失败，快照记为 `DELIVERY_FAILED`，`channel_bytes` 仍是 `UNKNOWN`。

## 组成摘要

在任务 `clone_path` 下读取 `AGENTS.md`、`CLAUDE.md`、`RULES.md`、`Agents.md`、`Claude.md`，以及最多 32 个 `.agents/skills/*/SKILL.md`。从 clone 根到文件的每一层只要是符号链接就跳过，因此 `.agents` 本身是链接时不会读到链接目标里的 Skill。Workflow 摘要来自状态库里可读的 workflow config。动态协议标记为 `OBSERVED`，并写上实际组装入口：回执路径是 `herdr.supervisor_delivery.deliver`，遗留路径是 `herdr.task_cli_execution.handle_dispatch_task`。存在 `working_context_id` 或 `context_id` 时，运行上下文标记为 `OBSERVED`，否则是 `UNKNOWN`。

厂商全局指令和模型内部 system prompt 固定为 `UNOBSERVABLE`。

## 读取、保留和完整性

默认读取走现有 `redact_text`，只返回 240 字摘录，并去掉传输证据里的 `detail`。完整正文、diff 和传输细节需要 `reveal=True`，CLI 对应 `--reveal`。访问边界是本机状态库加上这个开关，策略名是 `local_operator_reveal`。现有脱敏只覆盖已知密钥模式，不能把任意词都当成密钥。

`retain_until` 是创建时的整秒时间加 90 天，触发器禁止改这个字段。正文清空还要求它不晚于 SQLite 的当前整秒，并且状态和证据保持不变。每次打开状态库都会在同一个 `BEGIN IMMEDIATE` 里重建这三条快照触发器。替换失败会回滚，旧触发器还在；其它连接要等这次写入结束才能改快照。触发器同时拒绝 `DELETE` 和覆盖同一 `prompt_id` 的 `INSERT OR REPLACE`。`purge_expired` 只选择至少早了一秒的行，并按行保存点清空：清空 `prompt_content`，置 `content_purged=1`，保留哈希和状态。一行被拒绝不会回滚其它行。清除后校验返回 `HASH_RETAINED_CONTENT_PURGED`。V1 没有后台任务调用 `purge_expired`。

查询入口是 `bin/herdr-task prompt-ledger`，子命令为 `list`、`get`、`verify`、`diff`、`history`、`usage`。JSON 打到标准输出，参数错误退出码 2。

## 本版不覆盖

Coordinator 的 `/compact`、续跑通知、blocked SLA 重推、steering 注入，以及非 receipt-v1 的 supervisor 直接 `agent prompt`，仍不写 Ledger。生产 LaunchAgent 跑的是已安装快照，工作区改动不会自动生效。
