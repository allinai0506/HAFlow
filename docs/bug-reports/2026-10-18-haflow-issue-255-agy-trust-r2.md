# 缺陷复盘报告: haflow-issue-255-agy-trust-r2

- 任务标识: haflow-issue-255-agy-trust-r2
- 分支名称: agent/qodercli/fix-haflow-issue-255-agy-trust-r2
- 执行者: qodercli
- 生成日期: 2026-10-18
- 关联: issue #255 变更集（基线 8b581c3）在 cleanroom_review 门禁的唯一阻断项 B-1（候选 52f0369）

## 根因

`ensure_agy_workspace_trust()`（`herdr/workspace_trust.py`）以
`tmp.write_text(...) + tmp.replace(config)` 原子覆写
`~/.gemini/antigravity-cli/settings.json`。POSIX `rename/replace` 携带临时文件
自身的模式而非目标文件的模式：用户真实文件实测 0600，tmp 文件在 umask 022 下
创建为 0644，因此首次 agy 派发即确定性地把 0600 放宽为 0644，违反 AGENTS.md
§4.12 落盘安全边界。评审已在 temp HOME 用真实函数复现 0600→0644 并对真实文件
stat 取证。

深层原因：原子写模板从 grok/claude handler 复制时只覆盖了"内容原子性"，未覆盖
"权限保持"——tmp 与 config 位于同一 umask 目录，模式由创建时的 umask 决定。

## 防复发

1. **写前捕获、替换前 chmod**：写入分支先 `stat.S_IMODE(config.stat().st_mode)`
   捕获原模式，`tmp.replace(config)` 前 `os.chmod(tmp, mode)`；逐位保持原模式
   （非硬编码，0640 等同样保持）。
2. **首次创建约定**：目标不存在时按 0o600 创建（用户私有配置不继承 umask 宽松
   默认），与真实文件既有 0600 一致。
3. **回归测试即防线**：`tests/test_workspace_trust.py` 新增 3 个权限断言用例
   （0600+umask022 保持、0640 保持、首次创建 0600），覆盖 replace 携模回归。
4. 详细复盘见 [2026-10-18-haflow-issue-255-agy-trust.md](2026-10-18-haflow-issue-255-agy-trust.md)
   的 "B-1 返工修复" 小节。

## 验证与回归

- **TDD**：RED——3 个新用例先行失败（`420 != 384` / `420 != 416` / `420 != 384`，
  即 0644 对 0600/0640）；GREEN——修复后 `tests/test_workspace_trust.py` 16 passed。
- **真实函数复现**：临时 HOME + umask 022 直接调用 `ensure_agy_workspace_trust`：
  已存在 0600 → 操作后仍 0600 且内容合并正确；首次创建 → 0600。
- **内环目标套件**（test_workspace_trust / test_herdr_worker /
  test_worker_readiness_contract / test_preflight_runtime_contract /
  test_fix_1017_worker_rollback_clone / test_worker_startup_atomic_rollback /
  test_deep_preflight_accuracy）：**97 passed**。
- `python3 -m compileall -q herdr services bin` 通过；`ruff check` 改动文件与
  基线逐规则持平（无新增违规）。
- 未启动真实 Agent、未触碰真实 `~/.gemini`；实验全部在临时 HOME/临时目录。
- 已知限制：grok/claude handler 同型隐患未修（范围纪律仅 B-1 agy 路径），
  待后续独立小任务。

## 第三轮：Codex 对新 head 1a03e39 的 3×P1 返工（2026-10-18）

PR #259 第一轮返工（`f36f592`：fail-closed / flock+mkstemp / 中央 redactor /
证据保留命中行）合入推送后，Codex 在新 head 上再报 3×P1：

1. **短写即替换**：`os.write(fd, payload)` 返回值被忽略，ENOSPC 部分接受后照常
   `replace()`，把截断 JSON 覆盖到用户配置。修复：按返回值循环写满再 `fchmod`+`replace`。
   回归：`test_agy_short_write_is_completed_before_replace`（halving-write stub；
   RED 于 1a03e39：文件截断致 `json.loads` 失败）。
2. **截断先于脱敏**：`terminal_text = stdout[-4096:]` 的切点可落在
   `password=hunter2` 中间，残段 `word=hunter2` 丢失前导键后绕过逐行
   `redact_text` 随证据外泄。修复：超限切片后丢弃残缺首行。回归：
   `test_trust_tail_slice_does_not_split_credential_line`（精确构造边界；
   RED 于 1a03e39：`hunter2` 出现在 trust_evidence）。
3. **线程测试伪证跨进程锁**：`test_agy_concurrent_launches_keep_both_entries`
   双线程不控制交错，无法证明 flock 跨进程边界。补
   `test_agy_concurrent_processes_keep_both_entries`（fork + Barrier 同放两进程）。

**事故与处置**：编写并发测试期间发现测试自身每线程各自
`patch("pathlib.Path.home")` 的退出竞态曾向真实 `~/.gemini/antigravity-cli/settings.json`
写入 2 条临时目录条目（本文上方"未触碰真实 ~/.gemini"仅对更早轮次成立）。已精确剔除
这 2 条（其余 79 条与 0600 权限保持逐位不变），测试改为单一外层 patch；教训：涉及
HOME 重定向的并发测试必须进程级隔离，不能用同解释器线程 + patch 模拟。

验证：`tests/test_workspace_trust.py` + `tests/test_herdr_worker.py` 37 passed；
7 套件 106 passed；`compileall` 与 `git diff --check` 干净。
