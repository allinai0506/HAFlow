# Prompt Ledger

Prompt Ledger 保存 HAFlow 实际交给 Agent 传输通道的最终指令，并记录这份指令随后是已生成、已投递、已在终端通道核验、投递失败，还是无法核验。

代码事实来源：`herdr/prompt_ledger.py`、`herdr/state/schema.py` 的 `prompt_snapshots`、`herdr/supervisor_delivery.py`。设计说明见 `docs/architecture/prompt-ledger.md`。

## 查询

```bash
bin/herdr-task prompt-ledger list --task-id <task_id>
bin/herdr-task prompt-ledger list --workflow-id <workflow_id> --node-id <node_id>
bin/herdr-task prompt-ledger get --prompt-id <prompt_id>
bin/herdr-task prompt-ledger get --prompt-id <prompt_id> --reveal
bin/herdr-task prompt-ledger verify --prompt-id <prompt_id>
bin/herdr-task prompt-ledger diff --left <prompt_id> --right <prompt_id> --reveal
bin/herdr-task prompt-ledger history --prompt-id <prompt_id>
bin/herdr-task prompt-ledger usage --task-id <task_id>
```

默认输出不含完整 Prompt。`--reveal` 只适合本机操作者。`verify` 用保存的原文字节重算 SHA-256。正文过期清空后，哈希还在，但不能再重算，结果是 `HASH_RETAINED_CONTENT_PURGED`。

## 如何解读状态

`DISPATCHED` 表示 `herdr agent prompt` 或等价传输命令返回成功。`DELIVERY_VERIFIED` 表示未截断的终端缓冲里出现了同一份完整文本。两条都不表示模型已经收到或理解指令。模型内部 system prompt 和厂商 CLI 全局指令在组成摘要里是 `UNOBSERVABLE`。

同一任务的多轮返工各有自己的 `prompt_id`。相同 intervention 不会覆盖已有正文。

## 边界

Ledger 写入失败不改变原投递。回执事件里的 Prompt 副本仍可能变化，审计时以 `prompt_snapshots` 为准。保留期 90 天，清除要显式调用 `purge_expired`；当前没有定时任务。非回执的 supervisor 直发、steering 注入和 compact 续写不在本表中。
