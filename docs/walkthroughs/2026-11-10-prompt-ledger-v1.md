# Prompt Ledger V1 交付记录

日期：2026-11-10。沙盒分支 `feat/prompt-ledger-v1`，基线 `62078b280a6a4d8d18bc0f7f2bb677703e190acf`。本记录在独立审查完成前不把审查标为通过。

## 已经实现

- 状态库表 `prompt_snapshots`、不可变触发器、按 task 与 workflow/node 的索引，以及 intervention 唯一约束。建表发生在现有连接的 schema 初始化，没有第二套数据库。
- `herdr/prompt_ledger.py`：最终投递文本的 SHA-256、组成摘要、状态机、查询、diff、历史、usage、90 天保留清除。
- 回执投递 `deliver()` 在准备、待传输、成功、异常和已启动但结果不明五处旁路记快照。Ledger 失败不改变投递。
- 遗留无回执派发尾部按现有返回点记状态，不改变任务状态和退出码。
- receipt-v1 的 retry、verify、rework 传输命令按退出码、`agent_blocked` 和超时分类。
- CLI：`bin/herdr-task prompt-ledger` 的 `list`、`get`、`verify`、`diff`、`history`、`usage`。
- 架构说明 `docs/architecture/prompt-ledger.md`，知识页 `wiki/prompt-ledger.md`。

## 已经测试

2026-11-10 在沙盒 `/Users/user/.sandboxes/prompt-ledger-v1` 执行：

```text
python3 -m pytest -q tests/test_prompt_ledger.py tests/test_supervisor_delivery.py \
  tests/test_dispatch_delivery_timeout.py tests/test_task_delivery.py \
  tests/test_runtime_prompt_binding.py tests/test_delivery_rework.py \
  tests/test_completion_renewal.py
87 passed in 38.95s
```

`python3 -m compileall -q` 覆盖改动模块，`git diff --check` 无输出。测试覆盖短文本、20 万字以上长文本、`agent_blocked`、超时、Agent 未就绪、截断缓冲、`E2BIG`、同一 intervention 不覆盖、两轮返工都保留、传输中断后恢复、不同指令文件摘要、符号链接不跟随、脱敏、触发器拒绝改写、过期清空后哈希仍在、Ledger 故障不阻断投递。

同一命令里的既有投递、返工、回执和完成续期测试同时通过。没有在本机重跑全库 `pytest`。

## 已经验证

- 快照正文等于 `deliver()` 交给 send 的字符串，SHA-256 可由该字符串重算。
- 两轮不同 intervention 产生两行，diff 能看到两边正文。
- `before_prompt` 崩溃后第二次投递不重发，原文还在，状态成为 `DELIVERY_UNVERIFIED`。
- 命令成功记为 `DISPATCHED`，证据级别 `transport_command_accepted`，`model_receipt` 为 `UNOBSERVABLE`。
- 输入前拒绝记为 `DELIVERY_FAILED`；超时记为 `DELIVERY_UNVERIFIED`。
- 直接 `UPDATE` 正文会被触发器拒绝。

## 尚未验证

- 真实 `herdr agent prompt` / `pane send-text` 对运行中的 Agent。测试里的 send 是桩。
- 生产 LaunchAgent。服务跑的是已安装快照，本次没有重启。
- 全库 CI。
- `purge_expired` 没有被任何定时任务调用。
- 非 receipt-v1 的 supervisor 直发、steering 注入、coordinator `/compact` 和续跑通知不写 Ledger。

## 无法观测

- 模型是否收到或理解 Prompt。
- 厂商 CLI 的全局指令正文，以及模型内部 system prompt。组成摘要把二者标为 `UNOBSERVABLE`。
- 终端读回被截断时，通道里究竟有多少字节。证据记为 `UNKNOWN` 或 `pane_read_truncated`，不升为已验证。

## 已知限制

- 回执事件仍保留一份可改写的 Prompt 副本。审计以 `prompt_snapshots` 为准。
- `herdr/prompt_ledger.py` 超过仓库对 `herdr/` 核心文件 300 到 500 行的建议。它是一个模块，没有再拆。
- 默认查询只返回脱敏摘录。完整正文需要本机 `--reveal`。
