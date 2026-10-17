# Agent 执行轨迹可视化 V1 · 真实执行闭环修复 — 需求对抗审查与边界漏洞清单

状态：待需求所有者裁决。本清单是 `req-trace-interrogate-1016` 的破防性质询产出，与 `req-trace-spec-1016` 的需求规格互为质证。

## 0. 审查前提与取证方法

- 取证基线：`main` @ `812f451`，本工位克隆 `agent/qodercli/docs-req-trace-interrogate-1016`。
- 独立只读核对的源码：`herdr/native_session_trace.py`、`herdr/execution_trace.py`、`console/herdr_factory_console.py`、`tests/test_execution_trace.py`、`tests/test_execution_trace_api.py`、`tests/test_console_execution_trace.py`、`herdr/state_db.py`、`herdr/kernel.py`、`RULES.md`、`wiki/execution-trace.md`。
- 行为断言来源：除代码走读外，分类结果、重试链接、会话归属、扫描预算四项在 `/tmp` 沙盒内用只读探针实测（不落库、不起 Agent、不调模型）。实测数字在正文标注"实测"。
- 本清单不裁决业务语义：每条"建议裁决"只给出可验收的判定口径选项与不确认时的兜底后果，最终归属由需求所有者决定。
- 需求原文按 Task 契约收录的要点引用（含第九节验收、第十节禁止清单、第十一节测试要求）。规格草案 `execution-trace-v1-realloop-requirements.md` 由另一工位并行产出，本清单未读到其成稿；凡引用需求章节处，若成稿已定义，则以成稿为准并撤销对应质询。

### 0.1 前置事实纠正（影响整份需求的落点）

| # | 需求/契约假设 | 源码事实 | 后果 |
| --- | --- | --- | --- |
| F1 | 存在 `_classify_action`（`native_session_trace.py`） | 该文件无此函数；分类在 `herdr/execution_trace.py:489`，native 侧只有工具名白名单 `:19-20` | 按需求点名去 native 模块改"读取/编辑判定"会改错文件；真实闭环判定要同时动两个模块 |
| F2 | "真实执行闭环" = 六类事件都能从真实会话读出 | `file_read`/`file_edit` 仅来自 `read/read_file` 与 `edit/write/apply_patch/str_replace` 工具名；Codex/Claude 通过 shell 读写一律不产出这两类（实测 A） | 若验收写"六类事件在真实任务中均可见"，则对 Codex 结构性不可达 |
| F3 | `retry` 是原生会话事实 | 全仓 grep：`retry_of` 只有消费方（`native_session_trace.py:172,218,609`）与测试自造字段，无任何生产者 | 真实跑一次任何 Agent 都不会出现 `retry` 事件；B 层门禁必然取不到该样本 |
| F4 | "不自动建库/不触发迁移"待补 | `get_readonly_db_connection`（`state_db.py:1330-1357`）已 `mode=ro` + 文件必须存在，测试 `test_execution_trace_api.py:139-151` 已锁 | 把该条写成新增能力会产出"已满足"的伪改动；真正缺的是统一路径解析（见 Q-e1） |

---

## 1. 质询清单（按 acceptance 的 (a)–(i) 九个方向）

### (a) file_read / file_edit 证据判定边界

**Q-a1 — shell 读取形态的确证边界从未定义**

- 位置：`herdr/execution_trace.py:489-523`；`herdr/native_session_trace.py:19-20,662-681`；实测 A。
- 隐式假设：只要解析了 `command`，就能判定"读取了目标文件"；`cat/sed/head/tail` 与原生 Read 工具等价可信。
- 失败场景：实测 `cat src/billing.py` → `kind=tool_call, title=执行命令, detail='cat', path=None`；`sed -n '1,80p' src/billing.py`、`head -50`、`grep -n TODO src/app.py` 同样只有命令名、无文件。若需求声明"读取项目文件"覆盖 shell 读取，实现必须从 argv 猜被读文件：`cat $(ls)`、`find -name` 场景、`xargs cat`、`git show HEAD:file` 都无法确证，会把"读了别的文件"渲染成"读了这个文件"，即把推断当事实——直接违反 AGENTS.md 第 4 节约束 4（引用存在不等于结论已被证明）。
- 建议裁决：需求必须二选一并写进验收——(A) 口径收窄：`file_read` 只承认真生 Read 工具，shell 读取归入 `tool_call` 且不得显示文件；(B) 白名单确证：仅 `cat/head/tail/sed -n/rg/grep` 的"单一命令 + 单一位置参数 + 无重定向 + 文件在 clone 内"才产出 `file_read`，其余保持 `tool_call`，且事件必须带"推断"标记与 `source_ref`。若不选择，验收项无法判定通过与否。

**Q-a2 — 复合 shell 降级规则会吞掉可读证据（过度降级）**

- 位置：`herdr/execution_trace.py:36`（`_SHELL_SPLIT`）、`:493-496`；实测 A。
- 隐式假设：`_SHELL_SPLIT` 检测到的 `|`、`;`、`&&`、`&`、反引号、`$()`、换行一定出现在 shell 语法位置（引号外）。
- 失败场景：该正则不识别引号。实测 `rg "foo|bar" src/billing.py` → `title='调用工具', detail='exec', path=None`：连命令名都被丢弃，因为 `argv=[]` 后 `_display_command` 不再执行。任何 pattern/文件名里含 `|`、`;`、`&` 的单条命令都会退化成"调用工具 · exec"，页面看起来像"什么都没做"。反向也成立：`pytest -q 2>&1` 因 `(?<![>&])&` 未被判复合，实测归为可判测试（`test_execution_trace.py:158-165` 已锁定）。
- 建议裁决：需求需明确"降级"是安全侧行为还是缺陷。若要求降级可解释，验收应加一条：事件必须显示原始命令的可截断摘要（或 `command_omitted` 标记），不得只显示 `exec`；否则"轨迹完整"与"看不出 Agent 干了什么"两者不能同时成立。

**Q-a3 — 真编辑被漏报：写入型 shell 与生成型脚本完全隐身**

- 位置：`herdr/native_session_trace.py:662-681`（kind 只按工具名）；`herdr/execution_trace.py:501-504`；实测 A2。
- 隐式假设：Agent 修改文件只会通过 edit/write/apply_patch 工具。
- 失败场景：实测 `sed -i '' 's/a/b/' src/billing.py`、`echo x > src/generated.py`、`cp a.py b.py` 全部是 `tool_call`，`kind≠file_edit`。V1 若把"轨迹里看不到编辑"当作已修复，则对用 shell 写文件的 Agent（Codex exec、`python3 apply.py`）依旧漏报；用户会以为"这个 run 没改过代码"。
- 建议裁决：需求需声明 V1 的编辑证据边界（工具名编辑为唯一可确证来源），并把 shell 写入列入"已知不可见"而非"已覆盖"。若不声明，验收"所有编辑动作可见"不可证伪，属验收空洞。

**Q-a4 — `file_edit` 忽略失败信号：失败与部分成功的编辑显示为成功编辑**

- 位置：`herdr/native_session_trace.py:531-560`（Codex 两支 `is_error=False` 硬编码）、`:548-556`（仅单一 `exit_code` 才采用）；`herdr/execution_trace.py:501-504`（早于任何 `is_error`/`exit_code` 分支返回）；实测 B。
- 隐式假设：`apply_patch`/`Edit` 只要被调用就成功；失败已由 `is_error` 或退出码表达。
- 失败场景：实测 `Edit is_error=True` → `kind=file_edit, title='修改代码', status=info`；Codex `apply_patch` 输出是自然语言（"Success. Updated…" / "Failed to apply patch"），无结构化 `exit_code` → `exit_code=None, is_error=False` → 同样 `status=info`。即：一次失败的文件编辑在轨迹里与成功编辑逐像素相同，"测试失败 → 修好了 → 再测"的叙事被伪造成"改了代码 → 通过"。`apply_patch` 改 3 个文件其中 1 个失败时，整个调用仍是 1 条事件、无 per-file 状态（`:521-562` 无分块结构），"部分成功"在当前数据模型里根本不可表达。
- 建议裁决：需求必须明确 V1 是否承诺"编辑结果"语义。若承诺：验收需给出 Codex/Claude/OpenCode 各自的失败证据字段（Claude 有 `is_error`，OpenCode 有 `state.status`，Codex 只有文本）与取不到证据时的降级文案（如"编辑结果未知"），并要求 `file_edit` 不再无条件 `status=info`。若不承诺：需求与 wiki 的"不推断"章节要显式写明"编辑不显示结果"，否则测试即文档会把它读成已实现。

**Q-a5 — 敏感路径编辑被脱敏成"无主编辑"，闭环语义自相矛盾**

- 位置：`herdr/execution_trace.py:35,736-752`（`_SECRET_NAME` 命中即 `path=None`）；`_file_detail` `:755-756`；实测 B 第三行。
- 隐式假设：脱敏只需去掉路径字符串，事件本身仍具可读性。
- 失败场景：实测 `Edit .env` → `title='修改代码', detail='Edit', path=None`。用户看到"改了某个文件"却不知道是哪个；同时该事件仍计入 `file_edit` 使 `availability=agent_actions`（`:421-427`）。需求若写"轨迹不泄露敏感路径"且"每条事件可追责"，这两条在此处冲突：要么泄露，要么不可追责。
- 建议裁决：需求需指定第三种出口（例如显示 `path_redacted` 类别名或"配置类文件"标签，仍不落真实路径），并明确脱敏是否影响 `availability` 判定。业务语义（哪些算敏感）归属需求所有者，本清单不代为扩充 `_SECRET_NAME`。

### (b) retry_of 校验：自引用、跨会话、重复链

**Q-b1 — `retry` 没有上游生产者，整条验收链建立在自己造的字段上**

- 位置：`herdr/native_session_trace.py:172,218,609`；`tests/test_execution_trace.py:67,347,382,530`；全仓 `retry_of` grep 结果。
- 隐式假设：Codex/Claude/OpenCode 会在工具入参里写 `retry_of`（或 `retried_call_id`）。
- 失败场景：真实会话文件里没有这个字段（实测与 grep 双重确认：只有测试夹具写入它）。需求若把 `retry` 列为六类必达事件，并配 B 层真实验证，则该门禁永远取不到样本；写测试的 Agent 只能继续手写 JSON，形成"预制证据"（见 Q-g1）。
- 建议裁决：需求所有者确认 `retry` 是"上游事实（则须指出哪个 Agent 哪个版本真的写）"、"HAFlow 侧推断（则须定义推断规则并声明非事实）"还是"V1 非目标"。三选一前，验收不得把 `retry` 计入必达类。

**Q-b2 — 链接失败的三种情形处理不一致，且静默无提示**

- 位置：`herdr/execution_trace.py:362`（`retry_of in known and != source_ref`）；`herdr/native_session_trace.py:735-742`（`_known_retry`）。
- 隐式假设：拒绝非法引用即等于安全；用户不需要知道被拒绝过。
- 失败场景：实测 C 四种输入的结果——
  - 指向不存在的 `codex:OTHER:1` → 链接丢弃，事件仍是 `test_passed`，`degradation=[]`；
  - 自引用 `s1:3 → s1:3` → 同样静默丢弃；
  - 跨会话 `codex:s2:1 → codex:s1:1` → **被接受**，产出一条 `kind=retry`（"修复并再次测试"）；
  - 同会话合法链 → 正常。
  也就是说"不存在/自引用"被悄悄吞掉，"跨会话"反而通过。用户在页面上看不到"有一条重试声明没能成立"，把缺链接当成"没有重试"。
- 建议裁决：需求需定义非法 `retry_of` 的可见出口（建议保留原事件并附 `retry_unresolved` 降级码），并单列一条验收：三种非法输入都必须可被测试区分。业务上"跨会话是否允许互指"归属需求所有者：注意三个 Agent 的实现已经不一致（见 Q-b3），不定义就会长期分裂。

**Q-b3 — 同一语义在三个 Agent 上有两套校验规则（会话边界不同层）**

- 位置：Codex/Claude/OpenCode 解析期各自 `known_ids` 局部作用域（`native_session_trace.py:139-192`、`:195-238`、`:521-562`）；投影期 `execution_trace.py:347` 用合并后的全量 `source_ref` 集合再校验一次。
- 隐式假设：一次 `load_native_actions` 的会话集合是同一身份域，可互相比对。
- 失败场景：Claude/OpenCode 的重试链接在解析期判定，只认同一文件/同一 session 内的 id；Codex 的多会话（`MAX_FILES=8`，`:355`）合并成一个 actions 列表后才判定，于是同任务的不同 Codex 会话可互指（实测 C 第 3 行成立）。同一份需求描述"重试链接必须属于同一会话"时，三种 Agent 只有一种满足。
- 建议裁决：需求要么规定"重试必须同 session_id"（则 Codex 需改为解析期校验），要么规定"必须同 task（跨会话允许）"（则 Claude/OpenCode 需放开）。当前"按 Agent 不同而不同"不能算实现细节差异而豁免，因为页面文案是同一句"修复并再次测试"。

**Q-b4 — 重复 retry 链与链长无约束，序列号是合成值**

- 位置：`herdr/execution_trace.py:356`（`sequence = ordinal*2+1`）、`:367`（重试事件取 `max(sequence-1,0)`）。
- 隐式假设：ordinal 在同一批 actions 内可代表全局时序。
- 失败场景：实测 C：`codex:s2:1` 的 ordinal=1 得到 `sequence=2/3`，与 `codex:s1:*` 的 3/4/5 交叠；多会话合并后 sequence 完全失去单调含义（排序靠 timestamp 兜住，但 `timestamp=None` 时（`:759-764` 拒绝字符串时间）退化成按 id 字典序）。重复链 A→B→C→…长度不受限，且每条重试都会额外生成一个事件（`:364-373`），2000 条上限（`native:29`，`load_native_actions:91-93`）可能被重试事件占掉一半。
- 建议裁决：需求需说明"重试链"是否有深度/数量上限，以及 `sequence` 是否允许出现在页面排序逻辑之外的用途。若 V1 只保证"不伪造时序"，则需求应显式声明无时间戳事件不参与先后比较（现已有文案，`console:10942`），并删除对 sequence 的隐含时序承诺。

### (c) Commit 时间双语义在现有 status_history 下是否可区分

**Q-c1 — Git 提交生成时间在当前数据源里根本不存在**

- 位置：`herdr/execution_trace.py:267-274`（commit 事件仅取 sha，`timestamp=None`）、`:660-673`（`_commit_sha` 只读 `commit/integrated_commit/commit_result`）；实测本地库（507 tasks，92 条有 commit sha）：任务载荷中除 `updated_at/last_activity_at/sentinel_updated_at` 外无任何 `*_at` 与提交相关；81 条 `to=committed` 历史条目字段只有 `{status, at, from, to}`(+`reason/source/timestamp/runtime`)。
- 隐式假设：`status_history` 里已同时存有"Git 生成时间"和"状态转换时间"，只差展示层区分。
- 失败场景：需求若把双语义列为 V1 修复项，实现要么从没写过的字段里读（永远 `unknown`），要么新增 `git show -s --format=%cI` 之类外部调用——后者与第十节禁止清单（不得引入新副作用/外部命令）可能直接冲突，并且轨迹读取从此依赖仓库存在（clone 清理后不可读，见 Q-h1）。
- 建议裁决：需求所有者确认"Git 生成时间"的来源：(A) 明确声明 V1 只有状态转换时间，双语义降级为单语义 + "Git 时间不可得"文案；(B) 由已写 commit 的环节补字段（需说明哪个模块负责写、clone 清理后如何取）。若都不选，"双语义可区分"这条验收无对应数据源，属验收空洞。

**Q-c2 — `committed` 状态转换被改写为 `task_completed`，与 sha 事件构成两个"提交"呈现**

- 位置：`herdr/execution_trace.py:240-241`（`kind=="commit"` → `task_completed`/"已提交"）、`:320-321`（账本行同样改写）、`:267-274`（独立 `kind="commit"`/"生成提交"，无时间）。
- 隐式假设：一条 commit 语义只出现在一个事件上。
- 失败场景：状态历史里"已提交"（带转换时间、点为完成色）与载荷 sha 派生的"生成提交"（无时间、排到无时间区）同时出现，页面把同一事实拆成两条、且分处两个区块（`console:10929-10942`）。`test_missing_time_is_not_filled_and_similar_commands_are_not_retries`（`tests/test_execution_trace.py:172-180`）进一步断言：只有 `committed` 历史而无 sha 时，`kind != commit`——即"提交"事件的存在与否取决于载荷里有没有 sha 字符串，而非有没有提交事实。
- 建议裁决：需求需指定双语义的两条呈现各自归属（谁是权威、谁可缺省），并写明"92 条含 sha 的任务里有 14 条没有 `committed` 历史条目"（实测）这类覆盖不对称时的期望。否则 `test_execution_trace.py:142`（`events[-1]["kind"]=="commit"`）与页面区块顺序互相绑死，任何"双语义"改动都会撞既有断言。

**Q-c3 — 时间与状态去重依赖 float 完全相等，跨写入路径可能合成失败**

- 位置：`herdr/execution_trace.py:212-215`（`_transition_correlation` 用 `f"{float(timestamp):.6f}"`）、`:246-249,322-327`；`wiki/execution-trace.md`"同一次状态变化会同时写进状态历史和 `task_transition`…合成一条轨迹"。
- 隐式假设：状态历史条目与 `events` 账本行的 timestamp 逐微秒相等。
- 失败场景：两处分别取时间（历史写 `at` 或 `timestamp`，实测 1026 条只有 `at`、2571 条只有 `timestamp`、0 条两者兼具）；若某条转换只在其中一处留痕，或两次写入跨了微秒，`correlation_id` 不同 → 页面出现两条"任务已派发"。需求若把"同一次状态变化不重复显示"写成验收，就必须给出"重复"的判定口径（时间相等？还是因果同一？）。
- 建议裁决：需求需明确去重键（建议以状态机事件身份而非浮点时间，但业务口径由需求所有者定），并把"只有历史无账本行/只有账本行无历史"两种现场列为必测用例。当前 `tests/test_execution_trace.py:473-509` 只覆盖了两者时间一致的情形。

### (d) 刷新与分页竞态 — 判定依据缺失是否构成验收空洞

**Q-d1 — 需求写"旧响应不得覆盖新结果"但未给判定依据，现有实现只覆盖了一半场景**

- 位置：`console/herdr_factory_console.py:10954-10969`（`applyTraceResponse` 用 `taskTraceSeq` 与 `selectedTaskId` 双检）、`:10972-10986`（`loadTaskTrace`）、`:10987-11010`（`loadMoreTaskTrace`）、`:11069`（**只有切换任务才 `taskTraceSeq++`**）；`tests/test_console_execution_trace.py:93-96` 只测了"任务切换后的迟到响应被丢弃"。
- 隐式假设：`taskTraceSeq` 是"请求代次"，因此任何迟到响应都能被识别。
- 失败场景：同一任务内 `taskTraceSeq` 不变。用户点"加载更多"（请求携带 cursor=C，seq=S）后立刻点"重试"触发 `loadTaskTrace(tid, true)`（同样 seq=S）：两个响应都通过守卫，追加页响应会把旧 cursor 之后的事件 `concat` 到新首页之后（`:10963` 无按 id 去重），出现重复或错序条目；`model.appending` 只挡住"两次加载更多"，挡不住"加载更多 + 刷新"交错。
- 建议裁决：需求要么指定判定机制（推荐服务端返回可比较的代次令牌，或客户端每次请求携带自增 nonce 并在响应回来时比对 nonce 而非仅 seq），要么把验收改成可判定表述："同一任务内并发两个轨迹请求，后完成的旧请求响应不得进入渲染"。不给判定依据时，该验收无法通过也无法失败，属于验收空洞。

**Q-d2 — 响应载荷里没有任何可比较的版本/水位，cursor 只保证"存在"不保证"同一快照"**

- 位置：`herdr/execution_trace.py:197-209`（返回体无 revision/observed_at/candidate 版本）、`:402-418`（`paginate_trace` 要求 cursor 命中本次重建的列表，否则 `ValueError`）、`:430-459`（每次请求重扫 `events` + 原生会话）；`native_session_trace.py:73`（默认 4s 预算）。
- 隐式假设：分页是同一份快照上的连续切片。
- 失败场景：每次请求都重新扫描原生会话并重新合并；两次请求之间新事件到来、或第二次扫描被 `budget_s` 截断（实测：`native_index_budget` + `native_truncated` 在 2000 文件目录下 0.29s 即出现），翻页结果与首页不是同一集合：可能漏事件，也可能重复。cursor 仍"有效"（它在列表里），所以不报错——静默不一致。API 层只有 `limit`/`cursor` 非法返回 400（`:134-136` 测试），没有"快照已变化"的出口。
- 建议裁决：需求需明确 V1 的分页语义承诺：(A) best-effort 时间线翻页（则验收只要求"不崩不重"，并声明可能漏）；(B) 快照一致翻页（则必须引入服务端可比较水位，如 `run_id + 扫描截止时间 + 合并条数`，并定义 cursor 失效时的 409/重置行为）。业务承诺由需求所有者定；当前写法两者都不满足，无法验收。

### (e) 统一 state_db_path 与"不自动建库/不触发迁移"

**Q-e1 — 轨迹入口有第三套路径解析，与页面其余面板不同源**

- 位置：`console/herdr_factory_console.py:1893`（`HERDR_STATE_DB` 或 `Path(TASKS_FILE).parent/'state.db'`，`TASKS_FILE` 是模块常量 `:18,37` 指向 `~/.herdr-controller`）对比 `herdr/kernel.py:59-70`（`state_db_path()`：`HERDR_STATE_DB` > 仅当 basename 为 `tasks.json`/`workflows.json` 且父目录存在 > `get_default_db_path()`）对比 `herdr/state_db.py:1304-1327`（`resolve_state_db_path`：还认 `WORKFLOW_FILE/WORKFLOWS_FILE/CHECKPOINTS_DIR`）。同一文件其他接口都用 `state_db_path()`（`:68,440,458,488,509,1805…`）。
- 隐式假设：`api_task_trajectory` 的拼法与 `state_db_path()` 等价，因此"已统一"。
- 失败场景：运维把 `TASKS_FILE` 指到别处（多实例/受控测试环境）时，任务列表来自 A 库、执行轨迹来自 `~/.herdr-controller/state.db`（B 库）：轻则 404"任务不存在"（同页其他面板正常），重则串到同名另一任务。现有测试仅通过 `HERDR_STATE_DB` 走第一条分支（`test_execution_trace_api.py:97`），第二条分支无任何断言，"两套测试库"也证明不了优先级一致。
- 建议裁决：需求把"统一"落成可执行判据：轨迹入口改用 `state_db_path()`（或三者共同的单一权威解析），并新增一条现在就会失败的回归测试（设置 `TASKS_FILE` 指向临时库，断言轨迹读到的任务与列表一致）。同时需求需说明 `WORKFLOW_FILE/CHECKPOINTS_DIR` 两种历史入口是否在 V1 统一范围内，否则"统一"仍是三选二。

**Q-e2 — "不自动建库"已满足，但"不触发迁移"的验收证据只覆盖只读连接一处**

- 位置：`herdr/state_db.py:1330-1357`（契约：文件必须存在、`mode=ro`、`query_only`、不 init schema）；`execution_trace.py:430-434`（`FileNotFoundError` → `trajectory_unavailable`）；`tests/test_execution_trace_api.py:98-101,139-151`（monkeypatch 可写连接即抛 + 库文件 sha256/事件计数不变）。
- 隐式假设：只要轨迹代码不建库，整条链路就不产生迁移副作用。
- 失败场景：`get_readonly_db_connection` 的文档自己声明：WAL 模式下 SQLite 可能在连接生命周期内生成 `-wal`/`-shm` 协调文件（`:1347-1353`）。验收若写"无任何文件副作用"，则该断言在当前实现下不可成立（`sha256(state.db)` 不变不等于目录不变）。另外轨迹读取绕开 `get_state_store()`，一旦后续有人为复用能力而改回 `get_db_connection`，mkdir+建库+`_ensure_schema` 会立即回来——回归防护现在只有 monkeypatch 抛错这一层。
- 建议裁决：需求把"不自动建库/不触发迁移"的验收口径限定为可证伪形式（状态库文件内容哈希不变、目录内不新增应用级文件、不出现 schema 版本变化），或明确接受 `-wal/-shm` OS 级瞬态并写进条款。需求所有者需选择，两者不能同时省略。

### (f) 精确会话优先与 800 文件扫描预算的语义冲突

**Q-f1 — "定位已记录会话再套 800 上限"与实现不符：精确定位本身是无上限全树扫描**

- 位置：`herdr/native_session_trace.py:319-337`（`_find_recorded_codex` 两遍 `_walk_jsonl`，只受 deadline 约束）、`:346-351`（pinned 前置后再截到 `MAX_SCAN`）；文档承诺见 `wiki/execution-trace.md`："Codex 也先按已记录的会话 ID 定位，再套用最多 800 个文件的扫描上限"。
- 隐式假设：800 是本次读取的文件预算上限。
- 失败场景：实测（2000 个 filler 会话、`budget_s=4`）：`native_session_id` 指向不存在的会话时，`_codex_meta` 被调用 **2800 次**（第一遍 2000 次全树名扫描 + 第二遍对每个文件读 128KB 头 + 索引 800 次），同条件无 pinned 时只有 800 次——精确优先把预算放大 3.5 倍；两遍之间没有任何"已扫文件"复用。第二遍在旧会话（跨年份目录树）上尤其昂贵，因为它绕过了"新目录优先"的排序收益。生产机上 `~/.codex/sessions` 有历史全量时，一次刷新轨迹就可能长时间占 IO。
- 建议裁决：需求需明确 800 预算的作用域（索引扫描 / 单次轨迹请求 / 元数据读取次数），并给出 pinned 查找的独立上限与超时出口。若需求把"有界扫描"当作已完成事项引用现有 wiki，则文档与源码不符（AGENTS.md 第 0 节：文档与源码不符须记录差异）——本条即为该差异记录。

**Q-f2 — "会话身份可信"的定义已存在但覆盖不了真实工况，且失败是静默的**

- 位置：`herdr/native_session_trace.py:32-47`（`session_belongs`：`clone_path` 与 session `cwd` 必须相等；无记录 ID 时只看目录独占）、`:852-855`（`_norm_path` = `expanduser+abspath+normpath`，**无 realpath**）、`:35-37`（文档自述忽略 `agent_session_id`/pane id）；`:66-69,94-95`（拒绝注释）。
- 隐式假设：任务记录的 `clone_path` 与 Agent 自报的 `cwd` 是同一文件系统的同一表示；"可信"只需字符串相等。
- 失败场景：实测 F——clone 路径是符号链接时（`_norm_path(/tmp/probe-f-link)=/tmp/probe-f-link`，会话 `cwd=/tmp/probe-f-real`），会话被丢弃且只留 `native_unverified`；macOS `/var ↔ /private/var`、`/tmp ↔ /private/tmp` 正是这种结构，测试与生产都可能踩。另一侧：同一 clone 目录被复用（rework / 换 Agent 重启）时，`session_belongs` 认为目录独占即可信任，**上一 run 的会话被并入当前 run**（见 Q-h2）。即"身份可信"在两个方向上都不可靠：真实拥有的会被拒，历史拥有的会被收。
- 建议裁决：需求需定义"会话身份可信"的证据要求（建议要求 run 级凭证：`.herdr-launch-identity.json` 的 `native_session_id` + `run_id` 匹配才算可信，目录相等只作降级并在 UI 标明"按工作目录归属，未证明运行"）。是否接受符号链接场景需路径规范化决策（`realpath` 或双路径比对），属需求语义，本清单不代定。

### (g) 六类事件专项用例的原始证据来源

**Q-g1 — 现在六类里只有两类能追溯到真实格式，其余靠自造 JSON**

- 位置：`tests/test_execution_trace.py:47-91`（Codex 夹具：手写 `exec_command({cmd:...})` 字符串 + 自造 `retry_of` 字段）、`:330-354`（Claude 夹具：手写 `message.content` 块）、`:371-398`（OpenCode 夹具：手写 `part.data`）；`tests/test_console_execution_trace.py:59-68`（直接以 5 条硬编码事件做 UI 断言，含 `Read · src/billing.py`、`Commit · 9d41c07`）。
- 隐式假设：这些夹具形状即上游真实格式；UI 层用同一形状自证即算"专项用例覆盖"。
- 失败场景：Codex 真实会话的 `exec` 入参形态、`apply_patch` 输出文本、`session_meta` 字段若与夹具漂移（版本升级即会漂移），解析器静默产出 0 action（`parse_codex_jsonl:113-135` 只在"有坏行且无 action"时记 `native_corrupt`），而六类事件测试仍全绿。这正是需求文档预见的"预制 UI JSON 伪验收"批评：`test_console_execution_trace.py` 的 fixture 与后端返回结构同源手工书写，无法证伪结构漂移。
- 建议裁决：需求需规定专项用例的证据等级，并明确"最低可接受"来源——例如：(A) 每类事件必须至少一条来自本地真实会话文件的脱敏快照（记录来源 run/task 与 SHA256 指纹，落 `tests/fixtures/`）；(B) 允许手写格式但必须附"该形状来自哪个 Agent 版本"的可核查声明与 schema 漂移哨兵测试（真实文件存在时解析非空）。选 A/B 或都选属需求决策；不选则"六类专项"不可证伪。

**Q-g2 — 夹具把关键降级语义写成"应当成立"，反向锁死了修复方向**

- 位置：`tests/test_execution_trace_api.py:319-321`（对 codex 真实样本断言 `all(kind != "file_read")`）；`tests/test_execution_trace.py:117-124`（十类事件顺序断言含 `retry`）、`:154-166`。
- 隐式假设：现有"读不到 Codex 文件读取"是稳定期望。
- 失败场景：(a) 项若采纳"白名单确证 shell 读取"（Q-a1 选项 B），Codex 轨迹开始出现 `file_read`，`:321` 立即失败——需求没说明这条断言该改还是该保留，两个工位可能给出相反结论。同理，(b) 项把 `retry` 判为非目标后，`:121-122,362,395,541` 多处 `retry` 断言成为遗留包袱。
- 建议裁决：需求需显式列出"当前锁死旧行为的断言清单"及其处置（改/删/保留），否则验收第九节会同时要求"行为改变"和"测试不改"。本清单不代为决定哪条断言作废。

### (h) 不启动收费模型前提下 B 层真实产品验证的可达性

**Q-h1 — 现有 B 层门禁只在这台开发机上、且已经只剩一个可用样本，其他环境整段跳过**

- 位置：`tests/test_execution_trace_api.py:252-325`（依赖 `~/.herdr-controller/tasks.json`、`state.db` 与三个硬编码历史 task id；缺失即 `pytest.skip`；末尾 `assert proved_native`）；`_render` 无 node 即 skip（`:222-249`，`tests/test_console_execution_trace.py:48-50`）。
- 隐式假设：本机永远保留这三个任务、其 clone 目录、以及对应原生会话文件。
- 失败场景：实测当前状态——三个样本里 `wf-haflow-0923-02-impl-t2`（claude，唯一能证 `file_read`/`file_edit` 的分支，`:316-318`）与 `wf-nexusarchive-0917-01-requirements-challenger`（opencode，且 `run_id=None`）的 `clone_path` **已不存在**，只剩 codex 一个样本；而 codex 分支被 `:321` 禁止出现 `file_read`。也就是说，"真实产品验证"今天已退化为"单一样本 + 只见 tool_call"。任务归档或换机后整条测试静默 skip，CI 上从不执行（`RULES.md:49` 把全量交给 CI，而 CI 环境没有这些本机产物）。
- 建议裁决：需求需说明 B 层验证的承载位置与不可达时的出口：(A) 承认 B 层为"本机人工证据"，要求提交可核查快照（会话文件脱敏片段 + run/task 引用 + SHA256）作为验收物；(B) 由需求方指定可复现的受控采集方式（在隔离 clone 内跑一次既有 Agent 任务并留存会话文件到 fixtures，不新起收费模型调用以外流程）。"不起收费模型"与"必须真实会话"如何同时成立，是需求语义决策，本清单不代答。

**Q-h2 — 真实闭环最危险的后果：历史 run 的原生事件被盖上当前 run 身份**

- 位置：`herdr/execution_trace.py:610-631`（`_event` 默认 `run_id = task["run_id"]`，`:617`）；`:346-380`（原生事件全部经此构造）；`native_session_trace.py:357`（无记录 ID 时按目录独占接受同 clone 的任意会话）、`:91-93`；实测 D。
- 隐式假设：能读到的原生会话都属于任务当前这一次运行。
- 失败场景：实测 D——同一 clone 下两个会话（`sess-old`/`sess-new`，无 `native_session_id` 记录、目录独占）都被合并，两条 `file_read` 事件都显示 `run_id=run-NEW`，`degradation=[]`。rework、恢复派发、同任务二次执行都会命中：页面把上一次运行的读取/编辑当作本次运行的成果。这直接违反 AGENTS.md 第 4 节约束 2（不跨 Run 拼接）与约束 3（Trajectory 是历史事实），而 `test_execution_trace_api.py:301-303` 的断言 `run_id in (None, 当前 run)` 反而把该行为固化成期望。
- 建议裁决：需求必须给出"原生事件运行归属"的判定与出口（无 run 级证据时 `run_id=None` 并在 UI 标注"运行未证明"，或拒绝合并只显任务事件）。这是本清单里最可能导致错误结论的缺陷：不裁决即实施"闭环修复"，会把跨 run 拼接从缺陷变成验收要求。

**Q-h3 — 未支持的 Agent 没有任何降级说明，B 层对多数 Agent 结构性不可达**

- 位置：`herdr/native_session_trace.py:78-85`（只分派 codex/claude/opencode）、`:94-95`（注释仅在 `agent in {codex,claude,opencode}` 时补）。
- 隐式假设：Agent Pool 里都有原生会话适配器。
- 失败场景：实测 E——`agent=qodercli` → `actions=[]` 且 `notes=[]`，`availability` 退到 `task_events`（`execution_trace.py:421-427`）。工作流池含 qodercli/agy/pi/grok/kimi（`workflow.json` agent_policy），本任务自身即 qodercli：这些 Agent 的执行轨迹永远是"仅任务状态"。需求若把"闭环"写成全局目标而不区分 Agent 覆盖矩阵，则验收在本工位就无法自证。
- 建议裁决：需求需列出 V1 覆盖 Agent 清单与非覆盖 Agent 的指定文案（建议新增 `native_unsupported` 降级码，而非当前空 notes），并明确"未覆盖"算非目标还是算缺陷。

### (i) 第十节禁止清单与第九节验收、第十一节测试要求的相互冲突

**Q-i1 — 全量 pytest 的适用条件与"本地不重复全量"并存，需求引用时未带条件**

- 位置：`RULES.md:49`（非文档 PR 的全量由 CI 在合并结果上执行，不要求提交前/每轮修复本地重复全量；CI 不可用时须在隔离环境补证）；`AGENTS.md` 第 6 节（本地按真实影响面选专项路径，"本地不重复 CI 全量"）。
- 隐式假设：第十一节测试要求 = 本地跑 `pytest -q` 全量。
- 失败场景：两条口径同时执行时相互拉扯：全量本地跑会包含 `test_execution_trace_api.py:274` 这类依赖本机生产产物的测试（见 Q-h1），以及需要 node 的渲染测试；按 AGENTS.md 只跑专项，则第九节验收若写"全量通过"就不满足。更实际的冲突：本需求的改动会同时触碰被 `:321` 锁死的断言（Q-g2），全量本地跑会在无关用例上产生噪声。
- 建议裁决：需求需指明 V1 的门禁组合：受影响专项（execution_trace / native_session_trace / console 轨迹）+ CI 全量（不可用时补证），还是本地全量；并说明文档-only PR 的豁免是否延续。该选择影响工时与停表线，属需求所有者决策，不宜由实现侧默认。

**Q-i2 — "禁止写生产状态/禁止起真实 Agent"与"必须证明真实读取链"之间没有可执行出口**

- 位置：需求第十节禁止清单（按 Task 契约收录）；`AGENTS.md` 第 6 节（测试使用临时数据库/目录/受控环境，禁止写生产状态、启动真实 Agent、隐式调用收费模型）；现有链验证依赖真实产物：`tests/test_execution_trace_api.py:252-253,293`（读 `~/.herdr-controller` 真实库）、`native_session_trace.py:99-105`（`default_roots()` 指向 `~/.codex/sessions` 等真实目录）。
- 隐式假设：只要把测试夹具放进临时目录，就同时满足"真实调用链验证"和"禁止碰生产"。
- 失败场景：临时目录夹具验证的是解析器，不是"真实执行闭环"（真实闭环恰好要求生产会话格式）；一旦用真实 `roots()`，测试就依赖并读取生产会话文件（含潜在敏感内容），并且 `console` 的 `default_roots()` 不可注入时无法在 CI 复现。第九节要求"至少一条真实 CLI→核心→持久化→读取链"（AGENTS.md 第 6 节）与第十节禁止清单在本模块上的交集为空集，除非定义"真实"的证据等级。
- 建议裁决：需求需给出三级证据口径并各自指定期望结果（建议：L1 临时夹具=格式回归；L2 本机真实会话脱敏快照=闭环可达性；L3 生产只读抽检=上线后门禁），并声明哪一级是 V1 验收必需、哪一级可延后。不定义则"禁止清单"与"验收"至少有一条会被违反或被判为不可证。

**Q-i3 — 降级文案成为前后端隐式契约，测试要求与禁止清单都覆盖不到**

- 位置：`console/herdr_factory_console.py:10918`（`refused = notes.some(code==='native_unverified' && /独占|没有可核对/.test(message))`）；后端文案 `native_session_trace.py:61,64,68,95`。
- 隐式假设：`code` 是稳定契约，message 只是给人看。
- 失败场景：前端用正则匹配后端中文措辞来决定显示"原生会话无法确认属于当前任务…"还是"当前任务暂无可用执行轨迹"。四条 `native_unverified` 文案里只有两条命中该正则——`:95`（"没有找到可归属到该任务的原生会话"）被判成"非拒绝"，与 `:68`（目录不独占）显示不同。需求第十一节的测试与第十节禁止清单都不禁止"改文案"，于是任何措辞优化都可能翻转 UI 语义，且 `notes` 去重按 code 保留第一条（`native:869-878`、`execution_trace:772-781`），多因同码时用户只看到一条随机成因。
- 建议裁决：需求需把降级契约收敛到 `code`（建议拆码：`native_no_clone` / `native_shared_clone` / `native_no_matching_session` / `native_unsupported_agent`），并声明 message 不作为判定输入。业务上"每种成因显示什么文案"归需求所有者，但契约载体（code vs message）必须选定，否则第十一节的用例无法稳定断言。

---

## 2. 分级汇总

### P0 — 会导致错误结论或验收不可判定，实施前必须裁决

1. **Q-h2 历史 run 的原生事件被盖当前 run_id**（实测 D）：违反 AGENTS.md 约束 2/3，且现有测试把该行为固化为期望（`test_execution_trace_api.py:301-303`）。
2. **Q-a4 失败/部分成功的编辑显示为成功编辑**（实测 B）：`is_error` 被 `file_edit` 分支忽略、Codex 两支硬编码 `is_error=False`；"真实执行闭环"的最核心叙事（失败→修好→通过）被伪造。
3. **Q-b1 `retry` 事件无上游生产者**（全仓 grep + 实测）：六类事件必达的验收不可能满足；继续做只会产出预制证据（Q-g1）。
4. **Q-d1/Q-d2 刷新与分页竞态缺判定依据**：需求现写法既不能通过也不能失败，属验收空洞；实现侧的 `taskTraceSeq` 只覆盖"切任务"，不覆盖同任务刷新×翻页交错（实测代码路径）。
5. **Q-c1 Git 提交生成时间无数据源**（本地库实测 507/92/81 条统计）：双语义验收缺字段支撑。
6. **Q-g2/Q-i2 现有断言与禁止清单的冲突未列处置**：`test_execution_trace_api.py:321` 等断言锁死旧行为，与 (a) 的方向性修复直接对撞。

### P1 — 有明确失败场景，需在规格中定义口径或补回归

- **Q-e1** 轨迹入口第三套 db 路径解析（`console:1893`），`TASKS_FILE` 场景下与同页其他面板不同源；现在无测试覆盖。
- **Q-f1** 精确定位会话的元数据读取不受 800 预算约束（实测 2800 vs 800 次），与 `wiki/execution-trace.md` 的文档承诺不符。
- **Q-f2** 符号链接/`/var↔/private/var` 导致真实会话被静默拒（实测 F）；反向：目录独占即合并历史会话。
- **Q-a1/Q-a2/Q-a3** shell 读取/写入的确证边界、引号内 `|` 的过度降级（实测 `rg "foo|bar"` → 连命令名都不显示）。
- **Q-b2/Q-b3/Q-b4** 非法 `retry_of` 静默丢弃、跨会话规则三 Agent 不一致（实测 C）、sequence 合成语义与重试链无界。
- **Q-a5** `.env` 等编辑脱敏后成为"无主编辑"，仍计入 `agent_actions`。
- **Q-c2/Q-c3** `committed` 改写为 `task_completed` 造成同一提交两条呈现；correlation 依赖 float 完全相等（实测历史条目 `at`/`timestamp` 二选一，0 条兼具）。
- **Q-i3** 降级 code 复用 + 前端正则匹配中文 message 的隐式契约（实测四条 `native_unverified` 文案只有两条命中）。
- **Q-h3** 未支持 Agent（含 qodercli/agy/pi/grok/kimi）零 notes 零 actions（实测 E），覆盖矩阵未声明。
- **Q-h1** B 层门禁已退化为单一样本、其余 skip；CI 上从不执行。

### P2 — 建议记录，不阻塞

- **Q-e2** "不触发迁移"应限定为可证伪口径（`-wal/-shm` OS 级瞬态已在 `state_db.py:1347-1353` 自述豁免）。
- **Q-i1** 全量 pytest 的适用条件引用不完整（`RULES.md:49` 与 AGENTS.md 第 6 节的分工），建议需求原文补一句指向 `RULES.md:49`。
- 命名与文档纠偏：需求点名的 `_classify_action` 不在 `native_session_trace.py`（见 F1）；`_recent_jsonl`（`:241-278`）把"达到上限"与"超时"合并为同一个 `hit`，两者都归 `native_truncated`/`native_index_budget`，用户无法区分"库太大没扫完"与"太慢没扫完"。

---

## 3. 必须回问需求所有者的最小问题集

以下 10 问决定 P0/P1 能否收敛，业务语义不由本工位代答。

1. **运行归属**：当原生会话只有"目录相等"这一条证据时，V1 是否允许把该会话的事件标记为任务当前 `run_id`？若不允许，期望是 `run_id=None` + UI 标注，还是直接不合并原生事件？（对应 Q-h2，实测可复现）
2. **编辑结果**：V1 是否承诺显示文件编辑的成功/失败？若承诺，Codex（只有自然语言输出）在取不到结构化结果时期望文案是什么？`apply_patch` 多文件部分成功在 V1 是"不表达"还是"必须表达"？（Q-a4）
3. **retry 的地位**：`retry` 事件在 V1 是必达类、推断类（需声明非事实）还是非目标？若是必达类，请指明哪个 Agent/版本真的写 `retry_of`，以便取证。若为非目标，现有 4 处 retry 断言如何处置？（Q-b1、Q-g2）
4. **shell 读写边界**：`cat/head/tail/sed/rg/grep` 与 `sed -i/echo >/cp` 在 V1 是否进入"读取项目文件/修改代码"两类？若不进入，需求文案需改成"仅工具调用可确证"；若进入，确证的最小白名单形态是什么？（Q-a1、Q-a3）
5. **Git 时间来源**：Git 提交生成时间从哪取——补字段（谁写、clone 清理后怎么办）还是承认 V1 只有状态转换时间？双语义各自权威事件是哪一条？（Q-c1、Q-c2）
6. **竞态判定机制**："旧响应不覆盖新结果"按什么判定——客户端自增请求号、服务端代次令牌、还是快照水位？cursor 失效（快照已变）时期望返回 409、重置首页，还是继续 best-effort？（Q-d1、Q-d2）
7. **db 路径统一范围**：V1 是否要求 `api_task_trajectory` 改用 `state_db_path()`，并覆盖 `TASKS_FILE`、`WORKFLOW_FILE`、`CHECKPOINTS_DIR` 三种历史入口？"两套测试库"验收需要断言到什么强度才算证明优先级一致？（Q-e1）
8. **扫描预算语义**：800 上限约束的对象是"索引扫描文件数"还是"单次轨迹请求的元数据读取次数"？`native_session_id` 指向已消失会话时的时间/IO 上限与降级文案是什么？（Q-f1，实测 2800 次元数据读取）
9. **B 层证据载体**：在"不启动真实 Agent、不调用收费模型、不碰生产状态"的约束下，V1 的真实闭环验收证据由谁承载——本机脱敏会话快照入库、可复现采集流程、还是上线后只读抽检？现有三个硬编码历史 task id（其中 2 个 clone 已清理）是否算 V1 依赖？（Q-h1、Q-i2）
10. **降级契约与 Agent 覆盖矩阵**：降级判定载体是否统一为 `code`（前端不再匹配中文 message），每种成因对应哪个 code 与文案？V1 明确不支持的 Agent（qodercli/agy/pi/grok/kimi 等）算非目标还是缺陷，页面文案分别是什么？（Q-i3、Q-h3）

---

## 4. 自检与边界声明

- 本清单未修改任何业务代码；新增文件仅此一份。
- 全部"实测"结论可在 `/tmp` 隔离目录复现（构造会话文件 + 直接调用 `load_native_actions` / `_classify_action` / `project_native_actions`），未写入状态库、未启动 Agent 或 Pane、未调用任何模型接口。
- 未覆盖、因此也不算已质询的部分：`docs/product-specs/execution-trace-v1-realloop-requirements.md`（另一工位并行产出，本工位按契约不等待其成稿）；其成稿若已定义上述任一语义，对应质询应转为"与成稿一致性复核"，不视为已裁决。
