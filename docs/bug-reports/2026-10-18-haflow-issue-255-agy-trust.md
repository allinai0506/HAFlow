# 缺陷复盘报告: haflow-issue-255-agy-trust

- 任务标识: haflow-issue-255-agy-trust
- 分支名称: agent/qodercli/fix-haflow-issue-255-agy-trust
- 执行者: qodercli
- 生成日期: 2026-10-18
- 关联 issue: [#255 agy 新克隆目录信任对话框阻塞 worker 启动 / 派发回滚](https://github.com/allinai0506/HAFlow/issues/255)

## 根因

**直接原因**：agy（Antigravity CLI ≤1.4.2 实测）在从未信任过的目录首次启动时弹出交互式
信任对话框（"Do you trust the contents of this project?" / "> Yes, I trust this folder /
No, exit"），worker 的 `wait_startup_ready()` 经 `startup_readiness()` 分类为
`TRUST_REQUIRED`，`main()` 判定启动失败并回滚 pane、输出 `HERDR_WORKER_FAILURE=`。
洁净室/门禁每个任务都是新克隆目录，因此 agy 永久不可派发。

**深层根因（能力注册缺口，而非启动逻辑缺陷）**：`herdr/workspace_trust.py` 的
`_TRUST_HANDLERS` 只登记了 grok/claude 两个 handler；`bin/herdr-task launch` 在启动
worker 前调用 `preseed_trust_target()` 做前置预埋，但该函数对无 handler 的 agent 返回
None——agy 从未被预埋。派发链依赖"每个受支持 agent 都有 trust handler"这一隐式约定，
agy 接入时未补齐，且没有任何自动化检查暴露这一缺口。

**为何长期未被发现（预检 READY 与真实启动矛盾）**：`herdr/deep_preflight.py` 的 agy 探针
是 `agy --print` 无头模式，不触发信任对话框；`TRUST_PATTERNS` 虽能匹配 "do you trust"，
但预检路径根本走不到 pane 交互。于是预检 READY 与真实 worker 启动失败长期并存。

**环境陷阱（本次一并修复的正确性前提）**：agy 按"精确路径 + 大小写"匹配
trustedWorkspaces，而 macOS 上 Python `Path.resolve()` / `os.path.realpath` 不修正大小写
（`/users/USER/x` 原样返回），写入错误 casing 的条目等于没写。此前人工绕过时恰好输入了
正确 casing 才生效。

## 防复发

1. **能力补齐（路线 B：launch 前置预置信任）**：新增
   `ensure_agy_workspace_trust()` 并注册进 `_TRUST_HANDLERS`，使
   `preseed_trust_target(clone, "agy")` 自动生效。写入契约：
   - `os.path.realpath` 解析符号链接（agy 以 realpath 比对 cwd）；
   - 新增 `_exact_case_path()` 用 `os.scandir` 逐段重建真实目录项 casing；尚不存在的
     计划克隆叶子按"已存在前缀 canonical + 剩余段原样"拼接，保证 launch 可在目录创建前预埋；
   - 对 `~/.gemini/antigravity-cli/settings.json` 幂等合并：仅追加缺失条目，保留
     `trustedWorkspaces` 既有条目与全部其余顶层键，临时文件 + `replace()` 原子写。
   与 grok/claude 既有预埋模式一致，维持 worker 运行时只读用户信任配置的约定
   （`tests/test_herdr_worker.py` 的 grok/kimi 不写配置断言继续通过）。
2. **失败证据可判读（issue 方向 3）**：`services/herdr-worker.py` 在
   `TRUST_REQUIRED` 失败路径捕获有界（≤400 字符、单行、去 ANSI、key/token/secret
   脱敏）pane 文字片段：写入异常消息（`| trust evidence: …`）和
   `HERDR_WORKER_FAILURE` 负载的 `trust_evidence` 字段，替代此前仅剩
   "blocked during startup" 的不可判读证据；非信任类启动失败不伪造该字段（有测试断言）。
3. **回归测试即防线**：`tests/test_workspace_trust.py` 断言 agy 在
   `_TRUST_HANDLERS`/`preseed_trust_target` 覆盖、真实 casing、未创建叶子 canonical、
   幂等与保留其余配置；`tests/test_herdr_worker.py` 断言信任对话框文本 →
   `TRUST_REQUIRED` → 证据片段可识别对话框且有界。未来新增 agent 若无 trust handler，
   preseed 覆盖测试会提示缺口。

## 验证与回归

**TDD**：RED 先行——新增 8 个测试全部失败（agy handler/casing/幂等/preseed 覆盖
缺失、`trust_evidence` 缺失），失败输出已存档；最小实现后转绿。

**内环目标套件**（`python3 -m pytest -q tests/test_workspace_trust.py
tests/test_herdr_worker.py tests/test_worker_readiness_contract.py
tests/test_preflight_runtime_contract.py tests/test_fix_1017_worker_rollback_clone.py
tests/test_worker_startup_atomic_rollback.py tests/test_deep_preflight_accuracy.py`）：
**94 passed**，含 TRUST_REQUIRED 回滚语义与 worker 运行时不写信任配置的既有约束。
相邻套件 `tests/test_launch_reconcile_cli.py tests/test_agent_router_preflight.py
tests/test_worker_clone_hygiene_regression.py tests/test_worker_baseline_anchor.py
tests/test_worker_push_guard.py tests/test_simple_workbench.py`：60 passed。
`ruff check` 对改动文件与 main 基线逐规则持平（无新增违规）。

**真实链路验证（agy 1.4.2，本机 herdr 0.9.5 实时会话，独立临时 pane，未干扰工位）**：
1. 对照复现：全新目录 `/Users/user/issue255-verify/caseB2-untrusted`（不在
   trustedWorkspaces 及其任何受信任祖先下）直接 `herdr agent start --kind agy --
   --dangerously-skip-permissions`，pane 出现 "Do you trust the contents of this
   project?" 对话框；`startup_readiness()` 分类 `TRUST_REQUIRED`，
   `_trust_blocked_evidence()` 产出可判读片段（含对话框文字）。
2. 修复生效：同级全新目录 `caseA2-preseeded` 走真实调用路径
   `preseed_trust_target → ensure_workspace_trust(agents=['agy'])` 预埋后，同一命令
   启动返回 `interactive_ready: true`，就绪门分类 `READY`，全程无人工干预。
3. 真实配置幂等：对 `~/.gemini/antigravity-cli/settings.json` 连续三次预埋，条目数
   保持 1，其余 79 条人工条目与全部顶层键不变；验证后测试条目与临时目录已全部清理
   （恢复 79 条原状）。

**已知限制 / 未验证项**：
- `agy --print` 无头预检仍不触发信任对话框，deep preflight 的"新目录信任等待"对齐
  （issue 可选方向 1）未实施：修复后该矛盾不再阻塞派发，且实施需引入新的探针语义，
  风险大于收益。
- 对照案例中 `herdr agent start` 本身对 caseB2 也返回过 `interactive_ready: true`
  （对话框已渲染但 CLI 检测先到 idle），这正说明 worker 必须依赖 pane 文字就绪门；
  herdr 上游检测行为不在本任务修改范围。
- 自动应答路线（路线 A）未实施：路线 B 已被上述真实链路证据证明可靠，且符合
  worker 运行时只读约定，不再展开第二条路径。

## B-1 返工修复（cleanroom_review 门禁对候选 52f0369 的唯一阻断项）

**根因**：`ensure_agy_workspace_trust()` 沿用 grok/claude 的
`tmp.write_text(...) + tmp.replace(config)` 原子写模式，但 `rename/replace` 携带
**临时文件自身的模式**而非目标文件的模式。用户真实
`~/.gemini/antigravity-cli/settings.json` 实测为 `0600`，而 tmp 文件在 umask 022
下创建为 `0644`——首次 agy 派发即确定性地把 0600 放宽为 0644（评审在 temp HOME
用真实函数复现 0600→0644 并对真实文件 stat 取证），违反 AGENTS.md §4.12
落盘安全边界。grok/claude 两个既有 handler 存在同型隐患，但按范围纪律本次仅修
agy（见"已知限制"）。

**修复方式（方案 a：写前捕获、替换前 chmod）**：
- 写入分支内先 `config.stat()` 捕获原文件模式（`stat.S_IMODE`），在
  `tmp.replace(config)` 前 `os.chmod(tmp, mode)`，替换后权限与原文件逐位一致
  （非硬编码 0600，0640 等既有模式同样保持）；
- 目标文件不存在（首次创建）：`FileNotFoundError` 分支取 `0o600`——agy 配置属
  用户私有配置，首次创建不应继承 umask 的 0644 宽松默认，与真实文件的 0600
  约定一致；
- 捕获/设置失败落在既有 `(OSError, ValueError, TypeError)` 兜底内，行为与原
  写入失败一致（WARN + 返回 False），不改变语义。

**验证与回归（TDD）**：
- RED：新增 3 个用例先行失败并存档——
  `test_ensure_agy_workspace_trust_preserves_existing_0600_under_umask_022`
  （`AssertionError: 420 != 384`，即 0644 != 0600）、
  `test_ensure_agy_workspace_trust_preserves_other_existing_mode`
  （0640 被放宽为 0644：`420 != 416`）、
  `test_ensure_agy_workspace_trust_first_create_is_owner_only`（0644 != 0600）；
- GREEN：修复后 `tests/test_workspace_trust.py` 16 passed；
- 独立复现证据：临时 HOME 直接调用真实函数，umask 022 下已存在 0600 → 操作后
  `stat` 仍 0600 且内容合并正确；首次创建 → 0600；
- 内环目标套件（7 个文件）：**97 passed**；`compileall` 通过；`ruff check` 对
  改动文件与基线逐规则持平（无新增违规）。全程未启动真实 Agent、未触碰真实
  `~/.gemini`。

**已知限制**：`ensure_grok_workspace_trust` / `ensure_claude_workspace_trust` 的
同型 replace 携模隐患未在本次修复（范围纪律：B-1 仅点名 agy 路径），应作为后续
独立小任务处理。
