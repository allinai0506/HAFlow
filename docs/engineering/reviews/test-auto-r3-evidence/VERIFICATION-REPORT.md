# 验证报告 · 冻结候选 4e543ee（Agent 执行轨迹可视化 V1 真实执行闭环修复）

- 验证工位: wf-haflow-1016-02-test-auto-r3（task_type=test，agent=qodercli）
- 实现工位: impl-trace-core-1016-r2（agent=codex）—— 跨阶段 Agent 硬隔离已满足：验证由独立 Agent 执行，未复用实现阶段 Agent
- 冻结候选: `4e543ee6a9e9cc9d4269222a177a94aa12f78275`
- 基线核验: `herdr-task verify-baseline` → `BASELINE_MATCH`，`baseline_commit == head == 4e543ee`，`commits_ahead=0`，`worktree_changed=false`
- 验证方式: 只评不改。全程 `git diff` 为空，业务代码零改动
- 执行环境: 本工位隔离 clone `/Users/user/.herdr-controller/clones/wf-haflow-1016-02-test-auto-r3`，python3.14.7 / pytest 9.1.1 / ruff 0.16.8 / node 26.3.0
- 门禁结论: **blocked**（1 项阻塞缺陷 DEFECT-5 使既有守卫测试由绿转红；另有 3 项非阻塞缺陷 + 5 项观察，见第 3、4 节）
- 修订说明: 初版结论为 pass，依据是三个专项测试与 29 条探针；补充运行受影响控制台的既有守卫测试后发现候选引入红线，结论按证据下调为 blocked。测试节点无权修改业务代码，故退回实现工位修复。

## 1. 验收条目逐条判定

| # | 验收条目 | 判定 | 证据 |
| --- | --- | --- | --- |
| AC-1 | 验证对象必须是冻结候选 4e543ee（隔离 clone，baseline_commit 即该 SHA） | pass | `verify-baseline` → `BASELINE_MATCH`；`git rev-parse HEAD` = `4e543ee6a9e9cc9d4269222a177a94aa12f78275`；所有命令均在工位 clone 内执行 |
| AC-2 | 逐条机器判定 F1–F6 | blocked（F4 引入 DEFECT-5；F1 附 DEFECT-1） | 第 2 节 F1–F6 矩阵，29 条独立探针（`adversarial-probe.txt`）+ 真实语料复核 + node 级探针；F4 的 Console 轨迹样式使既有 design-token 守卫测试转红（`regression-spacing.txt`） |
| AC-3 | clone 内运行三个专项测试并记录逐条结果 | pass | `pytest -q tests/test_execution_trace.py tests/test_execution_trace_api.py tests/test_console_execution_trace.py` → **49 passed**，逐条 `-v` 清单见 `pytest-3files-v.txt`（49 条 PASSED，0 FAILED，0 SKIPPED） |
| AC-3b | 受影响面的既有回归测试（超出字面条目，按 `AGENTS.md` §6 补齐） | **blocked** | 引用被改 3 个源文件的全部 60 个测试文件 → `1 failed, 781 passed, 93 subtests passed in 182.39s`（`impact-regression.txt`，清单 `impact-files.txt`）。唯一失败 = `tests/test_console_standard_layout_tabs.py::test_no_spacing_grid_violations` 报 `['margin-right: 5px']`（`console/herdr_factory_console.py:3934`，`git blame` 归属本候选）；基线 `0ed2015` 同正则 0 命中 → 新增失败，即 DEFECT-5 |
| AC-4 | `compileall` 与定向 ruff | pass | `python3 -m compileall -q herdr services bin tests` → exit 0（`compileall.txt`）；ruff 逐规则 parent↔candidate 对比 → **0 新增**（`ruff-compare.txt`，差分脚本对 5 个改动文件逐规则计数，含 stdin 同源配置对比）；`git diff --check` → clean |
| AC-5 | 真实性三原则抽查 | pass | 无证据不生成事件：`cat x`（无 exit_code 的 test 命令）→ `tool_call`；无 SHA 的 committed → 不生成 commit 事件；时间未知不伪造：任务级 commit `timestamp=None`，Console 显示"时间未知"；失败不误报成功：exit_code=1 → `test_failed` + `status=error`；被拒补丁 → 不认领 `file_edit`（探针 `T.*` 全 PASS） |
| AC-6 | 改动文件域检查 | pass（文档域越界见 OBS-3） | 代码域完全落在白名单：`herdr/native_session_trace.py`、`herdr/execution_trace.py`、`console/herdr_factory_console.py`、`tests/test_execution_trace.py`、`tests/test_console_execution_trace.py`（`changed-files.txt`）；另有 3 个 docs 文件超出 AC6 字面白名单 |
| AC-7 | 不修改业务代码 | pass | 验证全程 `git diff --stat` 为空，`git status` 仅本工位新增的证据文件 |
| AC-8 | 输出 HERDR_GATE_VERDICT 并推进状态 | pass（结论为 blocked） | 输出 `HERDR_GATE_VERDICT: blocked`，随后 `herdr-task set ... agent_done --verdict blocked`，把 DEFECT-5 退回实现工位 |
| AC-9 | 产物落盘本工位目录 | pass | 全部记录在 `.herdr-test-evidence/`（见第 5 节清单） |

## 2. F1–F6 逐条判定

### F1 · Codex file_read / file_edit 保守识别 —— 判定：实现有效，含 2 项缺陷

- 认领链路可核查：`_actions_from_codex_calls` 要求补丁分支同时满足「可解包 + `_validated_patch` 结构完整 + 路径合法(≤512) + 无失败退出码 + `_patch_success`（exit_code==0 或输出恰为 `Success.`）」；读取分支要求命令为 `cat/head/tail/sed` 干净单文件目标，拒绝 heredoc、glob、重定向、变量、引号、反斜杠。复合命令按冻结断言不解释。
- 真实语料独立复核（只读，`~/.codex/sessions`，198 个 JSONL / 11494 个动作，`real-corpus-recount.json`）：
  `file_read=442`、`file_edit=46`、`test_passed=1`、`test_failed=2`、`retry=0`、`commit=0`；以更严格谓词复核认领项 → **可疑 0 条**。
- 交付文档声明 `file_read=619 / 18680 动作 / 203 文件`。本次复核为 442 / 11494 / 198：语料本身在移动，声明不可复算（见 OBS-2）。方向性结论一致。
- 边界探针（`adversarial-probe.txt`）：多文件 `cat`、glob、重定向、`$`、heredoc、`||`、超长补丁路径、补丁标记只出现在 output、畸形转义、NUL 字节 —— 除 DEFECT-1 外全部按保守语义降级。
- **DEFECT-1**：`||` 链首段被解释为 file_read（详见第 3 节）。当前真实语料 0 命中（`or-chain-scope.txt`：442 条 read 认领中 plain=435、`&&` 链=7、`||` 链=0）。
- **DEFECT-2**（基线既有，非本候选引入）：同一 session_id 跨多个 rollout 文件时 `source_ref` 冲突，`merge_trace_events` 静默丢弃动作。详见第 3 节。

### F2 · retry 语义准确化 —— 判定：pass

`_retry_title(previous, classified)` 由前后两侧 kind 定性，探针全 PASS：

| 前事件 | 后事件 | 标题 | 结果 |
| --- | --- | --- | --- |
| test_failed | test_failed | 修复并再次测试 | PASS |
| tool_call | test | 再次测试 | PASS |
| test_failed | file_read | 重试工具调用 | PASS |
| — | `retry_of` 指向自身 / 未知 ref | 不生成 retry 事件 | PASS |

另核验 `valid_retry` 的前置条件：previous 存在、同 session、previous.ordinal < current.ordinal、非自引用 —— 不满足则不标注 retry（不伪造配对关系）。

### F3 · Commit 时间-SHA 关联（时间未知不伪造） —— 判定：pass

- 无自带 SHA 的 `committed` 转换降级为 `task_completed`/"已提交"，不生成 commit 事件、不参与 `commit:{task}:{sha}` correlation（探针 `F3.shaless_committed_never_carries_task_sha`、`F3.shaless_becomes_submitted` PASS）。
- 任务级 SHA 单独成事件且 `timestamp=None`，排序落在无时间桶（`_sort_key` 第 0/1 桶），Console 显示"时间未知"（`F3.time_unknown_preserved`、`F3.unbacked_project_never_stamps_time` PASS）。
- 证据优先：`assemble_trace` 合并顺序改为 存储 → 原生 → 任务历史；带时间戳的转换与无证据投影共享 correlation 时，前者先入账并保留 110 时间，后者被去重（`F3.evidence_commit_wins_single_event` PASS：页面仅 1 条 commit，timestamp=110.0）。
- 生产写入侧核对：`bin/herdr-task` 用 `git rev-parse HEAD` 存 40 位完整 SHA，`commit_result` 取值为 `created/adopted/empty/refused` 字符串 —— `_SHA` 谓词不会把它们当成 SHA（`F3.adopted_not_a_commit` PASS）。不存在缩写/全称双格式导致同一 commit 重复成事件的路径。

### F4 · 执行中任务自动刷新 —— 判定：主路径实现，含 1 项生命周期缺陷 + 1 项阻塞红线（DEFECT-5）

- 已实现：`switchTaskDrawerTab('trace')` → `loadTaskTrace` + `startTaskTracePoll`（8s `setInterval(pollTaskTrace,8000)`）；切走 tab 或 `closeTaskDrawer` → `stopTaskTracePoll`；`pollTaskTrace` 每轮重新判定守卫。图标与"时间未知"在 `renderExecutionTrace` 内联。
- 交付测试用 node 探针手工把快照状态改为 `cleaned` 来证明守卫会停表 —— 这只证明守卫逻辑，不证明真实页面里快照会更新。
- **DEFECT-3**：`state.selectedTaskDetail` 只在 `openTaskDrawer` 内被赋值（console/herdr_factory_console.py:11122），全局 600s `refreshAll` 也不刷新它；`traceTaskWorking()` 读的正是这份快照 → 已完成任务的轮询永不停表；排队中（`pending`）任务转为 `working` 后自动刷新永不起动。node 级复现见 `poll-lifecycle.txt`：`{"loads":5,"cleared_after_completion":0,"poll_survives_finished_task":true}`、`{"auto_start_after_pending_to_working":false}`。

### F5 · 统一 state_db_path —— 判定：pass

- `console/herdr_factory_console.py` 三处 ad-hoc 解析（`api_workflow_recovery`、`api_workflow_recovery_decide`、`api_task_trajectory`）已统一到既有权威 `herdr.kernel.state_db_path`（第 25 行 import 为基线既有，非新建平行事实源）；全文件再无任何 `os.environ['HERDR_STATE_DB']` / `TASKS_FILE.parent/'state.db'` 片段（grep 命中 0）。
- 生产不漂移核验（LaunchAgent 实况）：`com.user.herdr-factory-console.plist` 仅设 `HERDR_ROOT/HOME/PATH`，未设 `HERDR_STATE_DB/TASKS_FILE/WORKFLOWS_FILE/CHECKPOINTS_DIR`。在该环境下三个解析器同值：`kernel.state_db_path()` = `get_default_db_path()` = `resolve_state_db_path()` = `~/.herdr-controller/state.db`（104MB 活跃库，与旧 ad-hoc 结果一致）→ 本候选不改变线上 Console 读取的库。
- 差异只出现在 env 被显式设置时：新解析尊重 `TASKS_FILE/WORKFLOWS_FILE` env，旧 ad-hoc 恒用模块常量 `ROOT`。方向与 `bin/herdr-task._get_store` 的写入侧选择一致（即 F5 的目的）。交付文档已如实记为"已知问题"。

### F6 · 精确会话不被扫描预算饿死 —— 判定：pass

- 机制：`_find_recorded_codex` 先按 `{recorded}.jsonl` 后缀定位、再用 meta 的 `session_id` 证实；`_load_codex` 把命中文件插到候选队首并按 `MAX_SCAN` 截断 → 精确会话不依赖 `MAX_SCAN=800` 窗口的位置。
- 独立复现（`adversarial-probe.txt`）：构造 820 个更新的噪声会话把精确会话挤出 `MAX_SCAN` 窗口，`load_native_actions` 仍返回该会话的 `file_read` 动作 → PASS。
- 抗饿死的失败边界同样有界：`_safe_recorded_id` 拒绝 `../`、`\`、超长 id → 路径穿越式 recorded id 不绑定任何会话（PASS，返回 0 动作）；预算压到 0.2s 时按 deadline 退出并给出 note，不挂起、不抛异常（PASS，实测远小于 8s）；meta 解析失败（session_id 为空）的文件在 `_load_codex` 的 `and session_id` 处被丢弃，不会凭文件名绑定他人会话。

## 3. 缺陷清单（含复现步骤）

> 阻塞项：**DEFECT-5**（候选使仓库既有守卫测试转红）。DEFECT-1…4 为非阻塞。

### DEFECT-1 · `||` 链首段被保守性不足地认领为 file_read（P2，本候选引入，真实语料当前 0 命中）

- 位置：`herdr/native_session_trace.py:700-710`（`_recognized_read_path`）
- 触发条件：Codex exec 命令含 `||`（或混合链 `A && B || C`），首段是干净的 `cat/head/tail/sed` 单文件读取，输出 blob 恰有 1 个 `exit_code` 且为 0
- 实际后果：产出 `file_read` 事件并给出首段路径。但 `A || B` 的整体 exit 0 只说明"某个分支成功"，首段完全可能失败（正因失败才走回退）→ 违反"无证据不生成事件 / 链式命令一律不解释"（`docs/walkthroughs/20261017-impl-trace-core-1016-r2.md` 防复发第 2 条与决策 1 的表述与代码不符）
- 复现：`cd <clone> && python3 .herdr-test-evidence/adversarial_probe.py` → `FAIL F1.or_chain_head_unclaimed[DEFECT-1]`、`FAIL F1.mixed_chain_head_unclaimed[DEFECT-1]`；夹具为 `cat missing.txt || cat real.txt` + `{"exit_code":0}`，实际认领 `missing.txt`
- 建议修复：判据从"`||` 是否出现在首个运算符之前"收紧为"整条命令不含 `||`"；一行改动，回归补 `cat a || cat b` 与 `cat a && rm x || cp y z`
- 规模测量：`.herdr-test-evidence/or_chain_scope.py` → `total=442 plain=435 and_chain=7 or_chain=0`

### DEFECT-2 · 同一 Codex session_id 跨多文件时 source_ref 冲突，轨迹静默丢事件（P2，基线 0ed2015 同样存在，非本候选引入）

- 位置：`herdr/native_session_trace.py:867`（`source_ref = f"{prefix}:{session_id}:{ordinal}"`，ordinal 每文件从 1 重启）+ `herdr/execution_trace.py:416-432`（`merge_trace_events` 按 `(source, source_ref)` 先到先得）
- 触发条件：一次 Codex 会话被写入多个 rollout 文件（真实存在：128 个 session_id 中 19 个，约 15%），且这些文件的 cwd 等于任务 clone
- 实际后果：`_load_codex` 解析全部文件（上限 `MAX_FILES=8`），但后文件的动作因 ref 冲突被丢弃 → 可视化少报 Agent 真实动作，且保留者是文件名倒序（较新文件），非时间序
- 证据：`ref_collision-scope.txt` —— 最严重会话 779 条解析动作中 354 条 ref 冲突；端到端 `loaded_actions=397 unique_refs=243`，`dropped_by_ref_collision=154`。HTTP 层复现：`PYTHONPATH=. pytest -q .herdr-test-evidence/probe_http_malformed_patch.py` → `DEFECT-2: 1 of 4 actions visible`
- 建议修复：把文件身份纳入引用（`codex:{session_id}:{file_sha7}:{ordinal}`）或改为会话内全局单调 ordinal；属实现核心层，建议转 implementation backlog
- 为什么未阻塞本门禁：基线既有、非本候选改动引入、影响面为可视化完整性而非状态/持久化/安全；但它使 F1 的交付统计口径（按文件解析）与真实下发口径（经 loader+merge）不一致，必须记录并由 wrapup 转接

### DEFECT-3 · Console 轨迹轮询读状态快照，停表/起动都不成立（P2，本候选引入，属 F4 范围）

- 位置：`console/herdr_factory_console.py:10981-10996`（`traceTaskWorking` 读 `state.selectedTaskDetail`）与 `11122`（唯一赋值点，只在 `openTaskDrawer`）
- 触发条件 A：抽屉停在"执行轨迹"页签、任务随后完成 → 状态快照永远是 `working` → 每 8s 一次 `/api/task/trajectory`（后端每次带 4s 预算扫真实原生会话）永不停止，直到用户切页签或关抽屉
- 触发条件 B：任务尚在 `pending` 时打开 trace 页签，之后转为 `working` → 轮询永不起动，"执行中自动刷新"对该常见路径失效
- 复现：`python3 .herdr-test-evidence/poll_lifecycle_probe.py` → `{"loads":5,"cleared_after_completion":0,"poll_survives_finished_task":true}`、`{"auto_start_after_pending_to_working":false}`
- 建议修复：`pollTaskTrace` 内一并轻量刷新任务状态（或复用 `/api/task/projection`），或在 `applyTraceResponse` 里以事件侧的 availability/终态收口；起动侧可在每次轮询 tick 重新判定 `startTaskTracePoll`
- 缓解因素：Console 仅监听 127.0.0.1 单用户本地；无状态写入、无越权；用户切页签即停

### DEFECT-4 · 强刷整块重渲染，丢失用户展开态并闪烁加载态（P3）

- `loadTaskTrace(tid, true)` 先把 `state.taskTrace` 置为 `{status:'loading'}` 并 `renderTaskDrawer()`（console:11001-11002），轮询每 8s 重建 `.td-ev` DOM → 用户已展开的事件明细（`toggleTraceEvent` 的 `hidden` 切换）被收起，页面闪一次"正在加载执行轨迹…"
- 复现：Console UI 上展开任一事件明细，等待下一次轮询即观察到收起（本轮未做浏览器实机验证，判定依据为 DOM 重建路径）

### DEFECT-5 · 候选引入禁用 spacing token，使既有守卫测试由绿转红（P1，阻塞，本候选引入）

- 位置：`console/herdr_factory_console.py:3934`，本候选新增的 `.td-ev-icon` 规则里 `margin-right: 5px;`
- 触发条件：执行仓库既有设计令牌守卫 `tests/test_console_standard_layout_tabs.py::TestConsoleStandardLayoutAndTabs::test_no_spacing_grid_violations`（禁止 5/6/7/9/10/11/13/14px 的 padding/margin/gap）
- 实际后果：`1 failed, 14 passed` → 4e543ee 使测试套件变红，属**新增失败**（baseline `0ed2015` 同一正则 0 命中，`git blame` 该行归属 `4e543ee` 本身）。CI 全量门禁必然失败；任何以"pytest 全绿"为前置的下游节点都会被卡住
- 复现：
  1. `git rev-parse HEAD` → `4e543ee6a9e9cc9d4269222a177a94aa12f78275`
  2. `PYTHONPATH=. pytest -q tests/test_console_standard_layout_tabs.py` → `1 failed, 14 passed`，`AssertionError: Lists differ: ['margin-right: 5px'] != []`
  3. `git blame -L 3934,3934 console/herdr_factory_console.py` → 归属本候选
  4. 基线/候选同正则对比 → `baseline violations: []` / `candidate violations: ['margin-right: 5px']`
  - 完整证据：`.herdr-test-evidence/regression-spacing.txt`、`impact-regression.txt`
- 影响面全量定向回归：引用被改 3 个源文件的全部 60 个测试文件 → `1 failed, 781 passed, 93 subtests passed in 182.39s`，唯一失败即本项（`impact-files.txt` 为清单）
- 建议修复（本工位只评不改，未执行）：`.td-ev-icon` 的 `margin-right` 改为 token 允许值（4px 或 8px）或复用既有变量；改后重跑该守卫测试与影响面回归
- 为什么阻塞：`AGENTS.md` §8 完成条件要求"回归通过、无已知阻塞缺陷"；本项是候选自身造成的可机器复现红测，不是环境噪声，也不是基线债务

## 4. 观察项（非缺陷，交由负责人决定）

- OBS-1 · HTTP 兜底仍会外泄内部错误串：`Handler.do_GET` 的 `except Exception as e: send_json(500, error=e)`（console:12200-12201）以及 `api_task_trajectory` 的 `except ValueError as exc: TraceQueryError(400, str(exc))`（console:1913）会把任意 Python 错误串写入响应体。B-1 要求的"畸形补丁路径零内部错误串"已在解析层达成并被 HTTP 级探针证明（200，无 `Begin Patch/invalid escape/Expecting value/Traceback/SECRET_PROMPT`）；但兜底层不加收敛，任何**新**异常类型（如 `tasks.version` 列被写坏时 `int()` 抛的 ValueError）仍会泄漏。属全 Console 既有面，非本候选引入。
- OBS-2 · 真实语料统计不可复算：交付给出 619/46/2/1/0/0，未附语料快照清单或哈希。本次同机复核得到 442/46/2/1/0/0（文件数 198 vs 203），`file_edit/test_*/retry/commit` 四类完全一致，`file_read` 随语料漂移。建议把"参与统计的文件清单 + sha256"或固定夹具入库，使"识别是否有效"成为可复查数字。
- OBS-3 · 文件域字面越界：`docs/bug-reports/2026-10-17-impl-trace-core-1016-r2.md`、`docs/walkthroughs/20261017-impl-trace-core-1016-r2.md`、`docs/walkthroughs/README.md` 不在 AC-6 的代码白名单内。这与 AC-9（返工记录必须落 `docs/walkthroughs/*r2*`）和仓库 `AGENTS.md` §8 知识沉淀要求是既有授权冲突，实现工位曾据此上报 blocked（`finding:fnd_02d65cc7769c4edb`），主控以把文档一并纳入 `4e543ee` 冻结候选的方式事实裁决。本项为纯文档，无运行时面，不计为阻塞缺陷；建议 AC-6 显式写明文档域豁免，避免下轮重复争执。
- OBS-4 · 34/442 条 file_read 认领缺少成功证据（`exit_code` 未知，仅"命令被发出 + 目标是干净单文件"）。事件 `status` 为 `neutral`、标题为"读取项目文件"，未误报成功，语义可辩护；与 test_* 要求退出码的严格度不一致，是否收紧交由负责人决定。
- OBS-5 · 环境越界发现（与本候选无关，仅提示）：`~/Library/LaunchAgents/com.user.herdr-controller.plist` 的 `EnvironmentVariables` 以明文存放第三方 API key。本次核验只读取了变量名、未记录取值，任何产物中都不含该值；建议单独立项处理。
- OBS-6 · 工位自检门禁对 test 节点结构性不可达（harness 侧，非本候选缺陷）：`.herdr-loop/EVALUATOR.sh` 跑全量 `pytest`（4598 items），而 `herdr/evaluator.py:131` 把评估器超时硬编码为 `timeout=300` 秒；本机实测全量约 28–35 分钟（`full-suite.txt`：13% 用时约 10 分钟），必然 `evaluator_exit:124` → `missing_exit_receipt:TEST_EXIT` → correctness 0.0，与代码好坏无关。叠加"只评不改"节点无权修复红测，`herdr-loop eval` 在本节点不可能达到 100.0。建议：评估器按节点 DoD 的专项命令取值，或把超时做成可配（如 `HERDR_LOOP_EVAL_TIMEOUT`），或给 test/评审类节点单独的收敛口径。本项只作观察与熔断依据，不改 harness。

## 5. 回归与产物清单

全部落在工位目录 `.herdr-test-evidence/`（未触碰来源项目目录，未启动真实收费 Agent，未改生产 Console/LaunchAgent/state.db）：

| 文件 | 内容 |
| --- | --- |
| `pytest-3files.txt` / `pytest-3files-v.txt` | AC-3 三个专项测试：49 passed 汇总与逐条 PASSED 清单 |
| `compileall.txt` | `compileall -q herdr services bin tests` → exit 0 |
| `ruff-compare.txt` / `ruff-candidate.txt` | 逐规则 parent↔candidate 差分（0 新增）与候选侧明细 |
| `real-corpus-recount.json` | 真实 Codex 语料六类命中独立复算 + 严格谓词零可疑 |
| `or-chain-scope.txt` | DEFECT-1 规模测量（`||` 链当前 0 命中） |
| `ref-collision-scope.txt` | DEFECT-2 规模测量（19/128 会话受影响，最严重 354/779 冲突） |
| `adversarial-probe.py` / `adversarial-probe.txt` | 29 条独立边界探针：26 PASS / 3 FAIL（对应 DEFECT-1、DEFECT-2） |
| `poll_lifecycle_probe.py` / `poll-lifecycle.txt` | node 级 F4 轮询生命周期复现（DEFECT-3） |
| `regression-spacing.txt` | DEFECT-5 阻塞项复现（4 步）+ 基线对比 + 影响面回归结果 |
| `impact-files.txt` / `impact-regression.txt` | 引用被改源文件的全部 60 个测试文件清单与执行结果（`1 failed, 781 passed, 93 subtests`） |
| `full-suite.txt` | 全量 pytest 进度实测（用于证明评估器 300s 预算不可行，见 OBS-6） |
| `probe_http_malformed_patch.py` | HTTP 级 B-1 零泄漏断言（pass）+ DEFECT-2 端到端复现（该文件刻意不使用 `test_` 前缀，避免被 CI 采集） |
| `verify_real_corpus.py` / `or_chain_scope.py` / `ref_collision_scope.py` | 上述三份统计的可重跑生成脚本（只读） |
| `part1.txt` / `part2.txt` | 已发布 checkpoint 分段的本地副本 |
| `changed-files.txt` | AC-6 文件域清单 |
| `VERIFICATION-REPORT.md` | 本报告 |

未验证项（如实声明）：全量 4598 用例未在本地跑完（单机约 28–35 分钟，见 OBS-6），已改为对引用被改模块的全部 60 个测试文件做定向全量并捕获唯一红测；其余用例与被改模块无引用关系，按 `AGENTS.md` §6 由 CI 在合并结果上执行。Console UI 未做浏览器实机验证（DEFECT-3 由 node 级页面生命周期探针复现，DEFECT-4 由 DOM 重建路径判定）；未发起任何真实收费 Agent 调用。

## 6. 受管发布与完成申报（收口状态）

| 阶段 | observation_id | sha256 |
| --- | --- | --- |
| 段落2 基线与专项 | obs_2b413d02746f467e9ee74dd7b394dc33 | e31fe82bca0911c8f53600b1dfc7fce2b5bbecb602e21371e4c7a53d2ace3139 |
| 段落3 F1–F6 判定 | obs_0dadba4e9a914bd489554f06e3755865 | 64c2d60acd4464eb97af69f84f18a6223137e65aa84e422931cc0137dc6c09f4 |
| 段落4 门禁修订（pass→blocked） | obs_eec504c4c0ae4402a3d8190963545cdc | dc771f53710cfa32174e42884a1547e5cbd30bc457a489d27a0a067dee378289 |
| 段落5 门禁与内循环收口 | obs_c576c5c4a14342f18c57f811ac84e226 | 8c9baba2767f95eb2046af919a19018ab826e131f02b464bef07a79b21f8b9b5 |
| 段落6 完成申报未落账 | obs_ce2dc22b5aab41adb9f2b42e040293c5 | a219e4f56a244e851f753a2111c14046b383161ba01c84aa5c3d1eceeb1fdfc8 |
| 聚合（6 段） | obs_3727a7172c1c45d997974c9ddcf2f4d3 | 1f7097bed1e96ea9d073507bade5633c80f55448014767d656ad791f69fa45ea |

- `business_acceptance_recorded`：event id 93154，verdict=blocked，AC-1..AC-9（AC-2=blocked），`required_criteria_sha256=de4e6a2b6be8803e82b658f0a78ce9c2e3a7831352100272ca3593eece28488d`
- 状态推进：`working -> agent_done -> completed`，`stage_verdict=blocked`，`stage_verdict_affected_task_ids=[impl-trace-core-1016-r3]`，`[VERIFIED CANDIDATE] head=4e543ee…`

### 本工位已知问题（过程缺陷，如实记录）
- KNOWN-1 · `report-completion` 未落账：本工位先执行 `set ... completed`（AC-8 要求推进状态），再执行完成申报，被 `herdr/completion_receipt.py:197` 以 `Task is not executing`（要求 status ∈ {pending,dispatched,working,rework}）拒绝。凭据本身有效（issued 1792196698 / expires 1792283098，未过期）。状态机无 `completed` → 执行态的回退路径，`renew-completion` 需主控签发 `--operation-id`，故不可本工位自纠。正确顺序应为 `report-completion` 先于 `set completed`。恢复路径见段落6。
- KNOWN-2 · 内循环未收敛（5/5 exhausted，score 50.0）：成因见 OBS-6 两个独立外部障碍（评估器 300s 硬超时 vs 全量 28–35 分钟；候选红测 + 只评不改无权修复）。已按熔断流程输出 `HERDR_TASK_BLOCKER`，未输出 `HERDR_TASK_DONE`。
