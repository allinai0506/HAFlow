# 缺陷复盘报告: wf-haflow-1022-01-implementation-hub-lifecycle-r6

- 任务标识: wf-haflow-1022-01-implementation-hub-lifecycle-r6
- 分支名称: agent/agy/fix-wf-haflow-1022-01-implementation-hub-lifecycle-r6
- 执行者: agy
- 生成日期: 2026-10-23
- 重放基线: 97dd736（merge 冻结候选 03699f79 与 origin/main f3aaf6ea）
- 来源版本: r5 93d72d4（commit 93d72d4d6e270f7381f96bd1b784f9fcb1f855d8）
- 任务性质: 前滚重放，口径未变

## 锁定口径
完全保持 issue #328 已锁定口径不变：
1. 退出码语义：exit 4=歧义/外来占用（`STATE_FOREIGN`）、5=不可验证（`STATE_UNVERIFIABLE`）、6=launchd托管拒停（`MANAGER_LAUNCHD`）、7=超时、2=用法错误。
2. 管理器三态：`unmanaged`、`launchd`、`unknown`。
3. 注册表键路径：`<CONTROLLER_DIR>/run/experience-hub/<pid>.json`。
4. stop 成功条件：所选 `(host, port)` 复探零 LISTEN。
5. 探活超时：探活子进程严格 2s 超时；缺失探活工具返回 5。
6. 安全脱敏：argv 严格脱敏 `--admin-token`/`--admin-token=`/`--token`；`--json` 输出不包含 argv。
7. 信号处理：v1 保持不注册 SIGTERM handler。
8. 副作用边界：status 不写任何文件；stop 只删除选择器命中的注册表条目，且从不删除 DB。

## 根因
1. **探针不可验证时伪造监听者证据（`_reprobe_listeners` 伪造 Listener(pid=-1)）**:
   - 在 `bin/herdr-experience-hub` 的 `_reprobe_listeners` 中，当 `lifecycle.run_lsof(port).kind == "unverifiable"` 时，原实现返回了合成的 `Listener(pid=-1, host="unknown", port=port, wildcard=False, is_hub=None)`。该行为违背了系统不变量（不可制造伪造证据），把探针本身的不可验证状态伪造成存在具体监听者的状态。
2. **进程存活状态判定将不可验证折叠为进程已死（`pid_alive` 二态折叠缺陷）**:
   - 在 `bin/herdr-experience-hub` 中，传递给 `stop_endpoint` 的 `pid_alive` 被实现为 `lambda pid: lifecycle.run_ps(pid).status == "alive"`。当 `run_ps` 探针失败返回 `status == "unverifiable"` 时，该表达式直接折叠为 `False`，等价于断言目标进程已死亡。
3. **`stop_endpoint` 误入 reoccupied 分支导致错误判定与伪造 PID 泄漏**:
   - 上述两项缺陷叠加时，`stop_endpoint` 轮询检测到 `alive == False` 且 `listeners` 存在元素（即合成的 `pid=-1` 监听者），错误地进入 `classify_reoccupied` 分支，返回 `EXIT_AMBIGUOUS (4)`、`STATE_FOREIGN`，并将伪造的 `pid -1` 输出到状态消息与 `--json` 报告中。根据 issue #328 既定规范，当探针或存活状态无法确认时，系统必须返回 `EXIT_UNVERIFIABLE (5)` + `STATE_UNVERIFIABLE`，保持 `reoccupied=False`、`timed_out=False`，不得追加信号，且必须保留注册表条目供人工排查。

## 防复发
1. **零伪造证据原则（No Fabricated Evidence）**:
   - `_reprobe_listeners` 在探针 `unverifiable` 时不再返回合成 `Listener(pid=-1)`，而是返回调用方可区分的不可验证信号（返回 `None`）。
2. **完整三态存活判定（Three-State Process Liveness）**:
   - `pid_alive` 探针回调保留 `run_ps` 的真实三态（`"alive"` / `"dead"` / `"unverifiable"`），不再折叠为布尔二态。
3. **`stop_endpoint` 失败关闭语义与不可验证通道（Fail-Closed on Unverifiable）**:
   - `stop_endpoint` 在轮询中检查 `probe_listeners` 与 `pid_alive` 的返回结果；一旦端点探针不可验证或目标存活状态无法确认，立即终止轮询并返回 `EXIT_UNVERIFIABLE (5)` + `STATE_UNVERIFIABLE`。
   - 明确状态契约：`reoccupied=False`、`timed_out=False`、不发送额外升级信号、不清理注册表条目（保留注册表文件）、输出文本及 JSON 中绝不出现伪造 PID。
4. **反例测试门禁（Adversarial Negative Tests）**:
   - 在单元测试 `tests/test_experience_hub_lifecycle_unit.py` 与 CLI 端到端测试 `tests/test_experience_hub_lifecycle_cli.py` 中补充覆盖 `lsof`、`ps`、`launchctl` 三条独立不可验证路径，断言退出码为 5、注册表保留且输出不含任何伪造 PID。

## 验证与测试结果
1. **测试套件执行**:
   - `pytest -q tests/test_experience_hub_lifecycle_cli.py tests/test_experience_hub_lifecycle_unit.py`
   - 测试结果：`110 passed in 16.52s`（全绿，未修改任何测试断言）。
2. **代码与静态质量**:
   - `ruff check --output-format json .`: All checks passed.
   - `python3 -m compileall -q herdr services bin tests`: 退出码 0.
   - `git diff --cached --check`: 无空白异常.
3. **改动路径自查**:
   - `bin/herdr-experience-hub`
   - `herdr/experience/hub_lifecycle.py`
   - `tests/test_experience_hub_lifecycle_cli.py`
   - `tests/test_experience_hub_lifecycle_unit.py`
   - `docs/bug-reports/2026-10-23-wf-haflow-1022-01-implementation-hub-lifecycle-r6.md`
   - 确认 `docs/operations/` 下任何文件均未被改动或回退。
