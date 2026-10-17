# 缺陷复盘报告: impl-trace-core-1016-r2

- 任务标识: impl-trace-core-1016-r2
- 分支名称: agent/codex/fix-impl-trace-core-1016-r2
- 执行者: qoder (r2 返工工位)
- 生成日期: 2026-10-17

上一候选 075fea1a 被评审门禁判 blocked，本报告记录四类缺陷（B-1、M-1/M-2、M-3、M-4）与从未实现的 T-CONSOLE 的返工。

## 根因

### B-1 — 补丁解包 json.loads 未捕获，畸形转义泄漏到 HTTP
- 直接原因：`native_session_trace.py` 在解析 Codex `apply_patch` 的字面量时用 `json.loads('"' + body + '"')` 解包 JS 字符串，遇到畸形转义（`\\q` 之类）时抛 `JSONDecodeError`，该异常未被 `load_native_actions` 的 `except (OSError, sqlite3.Error, TimeoutError)` 覆盖，冒泡到 Console，导致 HTTP 500/400 且把 Python 内部错误串（`Invalid \escape`、`Expecting value` 等）带进响应体。
- 深层根因：识别层把"能解包"当成前提，没有把"解包失败"建模成一条可降级的数据事实。外部会话日志是不可信输入，解包属于边界操作，必须在边界处捕获并降级。

### M-1/M-2 — 对真实 Codex 会话命中率为 0
- 直接原因：候选版本的识别逻辑基于假想格式（把 exec 命令直接当 argv、把 `*** Begin Patch` 当原始换行文本匹配），而真实 Codex rollout 是 `custom_tool_call` + `custom_tool_call_output`，exec 命令被包在 JS 源码 `tools.exec_command({cmd:"..."})` 里、patch 包在 `const patch = "*** Begin Patch\n..."` 的 JS 字符串字面量中，`\n` 是转义序列而非真实换行。
- 深层根因：识别器没有对着本机 `~/.codex/sessions` 的真实语料开发，正则与解包规则和实际日志结构脱节，导致 6 类事件全部漏识别。

### M-3 — retry 标题只取被重试类型
- 直接原因：`_retry_title` 只看当前事件的 kind，`cat` 失败后接 `pytest` 失败被标成"再次测试"，未综合前后两侧。
- 深层根因：retry 语义本质是"从 A 重试到 B"的配对关系，单侧定性丢掉了信息。

### M-4 — 无 SHA 的 committed 继承任务当前 SHA 且挤掉带证据转换
- 直接原因：任务历史投影对没有自带 SHA 的 committed 转换，回退到任务级当前 SHA，并用相同的 `commit:{task}:{sha}` correlation 参与先到先得去重；由于 `assemble_trace` 的合并顺序里无证据投影排在带时间戳的转换之前，带证据的转换（时间 110）被无证据项（时间 100）挤掉，时间被改写。
- 深层根因：correlation key 用了"继承来的"SHA，使无证据项与有证据项误判为同一事实；合并顺序把证据优先级放反了。

## 防复发

1. **边界即降级**：所有对外部会话日志的 `json.loads` 解包都用守卫包裹，畸形条目降级为 `tool_call`（不带 path/command、不带伪造语义），绝不向外抛内部异常串。回归：`test_malformed_codex_patch_literal_never_escapes_and_stays_unclaimed` 断言异常不逃逸且响应体无 `Begin Patch`/`invalid escape`/`Expecting`。
2. **对着真实语料开发识别器**：file_read / file_edit / test_* 的识别规则以 `~/.codex/sessions` 的真实 Codex rollout 为样例，要求正向证据（read 命令的干净单文件目标、apply_patch 的成功回执 `exit_code==0` 或 `Success.`）才认领事件，链式命令、heredoc、glob 一律不解释。回归测试覆盖真实格式的解包、认领与拒绝。
3. **retry 双侧定性**：标题由前后 kind 共同决定（两侧都是测试→"修复并再次测试"；仅当前是测试→"再次测试"；否则→"重试工具调用"）。
4. **证据优先且禁止继承 SHA**：无自带 SHA 的 committed 转换降级为 `task_completed`（"已提交"），不生成 commit 事件、不共享 commit correlation；`assemble_trace` 合并顺序改为 `存储事件 → 原生动作 → 任务历史`，带时间戳的证据转换不被无证据投影挤掉；时间未知保持未知。
5. **真实命中率留档**：以只读脚本对全量真实会话统计六类事件命中数与误报数（见"验证与回归"），把"识别是否有效"变成可复查的数字，而不是一句 PASS。
6. **T-CONSOLE 能力测试化**：state_db_path 权威解析、执行中轨迹轮询、六类 kind 图标、空时间显示"时间未知"都在 `tests/test_console_execution_trace.py` 有 Python 级与 node 级断言。

## 验证与回归

命令与结果（本地隔离仓库根目录，python3.13 + homebrew pytest）：

```
pytest -q tests/test_execution_trace.py tests/test_execution_trace_api.py tests/test_console_execution_trace.py
  → 49 passed
python3 -m compileall -q herdr console tests
  → OK
git diff --check
  → clean
ruff（白名单文件，对比 HEAD 的每规则计数）
  → 0 新增（native_session_trace.py 反而 -1）
```

真实 Codex 语料（`~/.codex/sessions`，203 个 JSONL，18680 个动作）只读统计，六类事件命中数 / 误报数：

| kind | 命中 | 误报 |
| --- | --- | --- |
| file_read | 619 | 0 |
| file_edit | 46 | 0 |
| test_failed | 2 | 0 |
| test_passed | 1 | 0 |
| retry | 0 | 0 |
| commit | 0 | 0 |

- 原始 pytest 打头命令 69 条，其中 29 条为 `&&` 复合（按设计不解释）、40 条单命令里 37 条输出被截断为无 `exit_code` 的纯文本（真实性三原则：无证据不生成事件），故 test_* 命中数低是保守正确，不是漏识别。
- retry/commit 命中 0 符合预期：Codex exec 会话不携带 `retry_of`，commit 事件来自任务历史/存储事件而非原生适配层。
- 误报数为 0：每条被认领事件都用一个独立的更严格谓词复核（read 命令确实是 cat/head/tail/sed 单文件；edit 确实有 apply_patch 成功回执；test 确实是测试二进制且退出码语义一致），无一例外。

未验证项：全量 pytest 由 CI 在合并结果上执行；本地仅跑受影响三文件专项。生产 Console/LaunchAgent 未改动，未做真实收费 Agent 调用。
