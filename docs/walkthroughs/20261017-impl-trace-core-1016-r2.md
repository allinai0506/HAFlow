# 2026-10-17 · impl-trace-core-1016-r2 执行轨迹识别返工交付记录

- 任务标识: impl-trace-core-1016-r2
- 分支: agent/codex/fix-impl-trace-core-1016-r2
- 工位: r2 返工（接替 codex 会话）
- 上一候选: 075fea1a（评审门禁 blocked）

## 一、任务目标与背景

上一候选被评审门禁判 blocked，四类缺陷 + 一项从未实现的能力需返工：

- **B-1**：`native_session_trace.py` 补丁解包 `json.loads` 无捕获，畸形转义让 `JSONDecodeError` 逃逸到 Console（HTTP 报错 + 泄漏内部错误串）。要求修复后 HTTP 响应零内部错误串，畸形条目降级为 `tool_call` 且不带伪造语义。
- **M-1/M-2**：对真实 Codex 会话命中率为 0，需以本机 `~/.codex/sessions` 真实日志重写识别，并交付六类事件在真实样例上的命中数与误报数。
- **M-3**：retry 标题只取被重试类型，需综合前后事件类型定性。
- **M-4**：无 SHA 的 committed 继承任务当前 SHA，且先到先得去重挤掉带证据转换（时间 110→100）。要求禁止继承 SHA、时间未知保持未知、带证据转换不被无证据挤掉。
- **T-CONSOLE**（从未实现）：Console 统一 `state_db_path` 权威解析、执行中轨迹轮询刷新、六类 kind 图标、空时间显示"时间未知"。

## 二、改动范围

白名单文件内（代码/测试），逐项先写可复现失败的回归测试再最小实现转绿：

| 文件 | 改动 |
| --- | --- |
| `herdr/native_session_trace.py` | 新增 JS 字符串字面量扫描、补丁解包守卫与成功回执校验、真实 read 命令识别（cat/head/tail/sed 单文件、拒绝 heredoc/glob/链式）；`_actions_from_codex_calls` 改为"命令先解包→命中补丁标记则走补丁分支，否则走只读识别"，畸形一律降级为裸事实。 |
| `herdr/execution_trace.py` | `assemble_trace` 合并顺序改为 `存储→原生→任务历史`（证据优先）；`project_task_history` 无 SHA 的 committed 降级为 `task_completed`、commit correlation 只用自带 SHA；`project_native_actions` 增加 commit correlation 与 `_retry_title` 双侧定性；移除链式 head 回退（复合命令按设计不解释）。 |
| `console/herdr_factory_console.py` | 三处 ad-hoc `HERDR_STATE_DB`/`TASKS_FILE` 解析统一为 `state_db_path()`；执行轨迹 `renderExecutionTrace` 内联六类 kind 图标、空时间显示"时间未知"；新增 `traceTaskWorking/pollTaskTrace/startTaskTracePoll/stopTaskTracePoll`，trace 页对 working/dispatched/rework 每 8s 强刷，切页/关抽屉即停。 |
| `tests/test_execution_trace.py` | 新增 9 条真实格式回归（B-1 三形态、read 识别、apply_patch 认领/拒绝、retry 双侧、SHA 不继承、证据优先、时间未知）。 |
| `tests/test_console_execution_trace.py` | 新增 state_db_path 权威解析（Python 级）、六图标与"时间未知"（node 级）、轮询守卫（node 级）与静态接线断言。 |

`tests/test_execution_trace_api.py` 未改动，保持全绿（codex 原生会话禁止 file_read 的冻结断言仍成立）。

## 三、验证与测试数据

```
pytest -q tests/test_execution_trace.py tests/test_execution_trace_api.py tests/test_console_execution_trace.py
  → 49 passed
python3 -m compileall -q herdr console tests → OK
git diff --check → clean
ruff（白名单文件，逐规则对比 HEAD）→ 0 新增（native_session_trace.py -1）
```

真实 Codex 语料只读统计（203 JSONL / 18680 动作），六类命中数 / 误报数：

| kind | 命中 | 误报 |
| --- | --- | --- |
| file_read | 619 | 0 |
| file_edit | 46 | 0 |
| test_failed | 2 | 0 |
| test_passed | 1 | 0 |
| retry | 0 | 0 |
| commit | 0 | 0 |

关键判读：
- 69 条 pytest 打头命令中 29 条为 `&&` 复合（按设计不解释），40 条单命令里 37 条输出被截断为无 `exit_code` 纯文本 —— 真实性三原则要求"无证据不生成事件"，故 test_* 命中低是保守正确。
- retry / commit 命中 0 符合预期：Codex exec 会话不携带 `retry_of`，commit 事件源自任务历史/存储事件，而非原生适配层。
- 误报 0：每条被认领事件都经独立的更严格谓词复核（read 是真单文件读；edit 有 apply_patch 成功回执；test 是测试二进制且退出码语义一致）。

## 四、决策与权衡

1. **复合命令不解释**：曾尝试把 `pytest && ...` 链首段识别为 test 事件，但与冻结的 `test_execution_trace_api.py` 断言冲突（`pytest -q && echo done` 必须保持 `tool_call`），故移除链式回退，命中全部来自单命令。代价是部分真实 test 漏识别，符合"宁可漏、不可伪造"。
2. **补丁成功回执为必要条件**：`file_edit` 认领要求 `exit_code==0` 或输出恰为 `Success.`，无回执（e3）与失败（e2）都降级，避免把"发了补丁"当成"改了代码"。
3. **图标内联而非独立函数**：Console 的 node 测试通过大括号计数抽取有限函数集合渲染，独立 `traceIcon` 会让冻结 API 测试的 `_render` 缺符号；改为在 `renderExecutionTrace` 内联图标映射，保持两处 harness 均可运行。

## 五、已知问题 / 未验证项

- 全量 pytest 由 CI 在合并结果上执行；本地仅跑受影响三文件专项。
- 生产 Console / LaunchAgent 未改动；未做真实收费 Agent 调用。
- `state_db_path()` 与旧 ad-hoc 解析在 `HERDR_STATE_DB` 未设且 `TASKS_FILE` 缺省时可能落到不同默认路径；测试用 `TASKS_FILE` 环境变量证明权威解析生效。
