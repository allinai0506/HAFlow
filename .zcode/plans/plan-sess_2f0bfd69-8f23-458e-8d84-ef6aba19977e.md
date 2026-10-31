# 恢复 PR #359（wf-1029 启动失败收敛）到 main

## Context

审计发现 PR #359 在 GitHub 显示 MERGED，但其内容不在当前 main（eb8d054）历史中。取证结论：

- **时间线**（commit 时间戳证实）：10-30 09:34 #358 合并（main=3c97080）→ **main 在 16:44 前被回退到 c97f302**（force-push，丢掉 #358 合并提交）→ 16:44 #359 合并（4262830，基于 c97f302）→ **main 再次被回退到 3c97080**（丢掉 #359，恢复 #358）→ 21:16/22:41 #360/#361 合并到不含 #359 的主线。
- **丢失面**：恰好只有 #359（12 个提交：wf-haflow-1029-02 联合修复候选 + display）。#358/#360/#361 内容都在 main；4262830 不含 #360/#361 的修复，两线从 c97f302 分叉、互补。
- **意图证据**：wiki/log.md 的 #359 侧已记录「启动失败收敛」交付条目，main 侧无任何删除/回退记录；`joint/wf-haflow-1029-02-*` 本地分支（含 workflow_startup.py）是被 herdr 工作流遗留的工作现场，不是主动回退。结论：**意外丢失（大概率是某次 stale main 的 force-push），需要重新落地**。
- **规模**：35 个文件、约 +1900 行（herdr/workflow_startup.py、services/controller/startup_watchdog.py、2 个测试文件 ~900 行、console 标签、factory 启动流程等）。
- **冲突面已逐一核实**：与 main 后续改动重叠的只有 4 个代码文件 + wiki/log.md（operations.js、herdr/agent_router.py、services/controller/advancement.py、services/herdr-controller.py），#359 侧在这些文件上共 45 行新增，且 main 上锚点（`startup_ready is False` 门、`set_workflow_agent_override` 旧两行、install_modules 列表）全部仍在。

## 目标 / 非目标

- 目标：把 4262830 的全部内容重新落到 main；顺带修掉 #359 上 2 条未解决评审缺陷（正是本次审计的欠账）；wiki/lessons 记录本次事故；给出 force-push 防复发证据与建议。
- 非目标：不动其余 34 条评审遗留缺陷（另批处理）；不动 open PR #362；不改 main 的 #360/#361 行为。

## 实施步骤（隔离分支，不直接在主干开发）

1. **取证补充（只读）**：`gh api repos/allinai0506/HAFlow/events` 查 10-30 的 force-push 记录（actor/时间），结果写进 PR 描述与 lessons——这是防复发的关键证据（同一机制可能再次吃掉重落地 PR）。
2. **建分支**：从 main 新建 `fix/reland-wf-1029-startup-convergence`。
3. **`git merge 4262830`**（保留真实历史，使 4262830 重新成为 main 祖先），解决冲突：
   - `services/controller/advancement.py`：保留 main 的 #360/#361 改动，把 #359 的 3 处接入（顶部 import `startup_wait_closed/startup_wait_hold`、`workflow_closed` 早退处加 `startup_wait_closed(ports, workflow_id)`、`startup_ready is False` 换成 `startup_wait_hold(...)`、DEPENDENCIES 加 `_startup_wait_log`）。**合并后 ~504 行，需精简约 4 行** 以满足 `tests/test_controller_modularity.py` 的 ≤500 行门禁（`test_declared_responsibility_dependencies_are_acyclic_and_resolvable`）。
   - `herdr/agent_router.py`：`set_workflow_agent_override` 采用 #359 版本（`update_workflow_metadata(..., expected_status="running")` 条件写）；`expected_status` CAS 参数由 #359 自带于 `herdr/state/transition_gate.py`（main 未动过该文件，干净合入，对 main 既有 3 参调用向后兼容）。
   - `services/herdr-controller.py`：合入 `StartupWaitLog` import、`_startup_watchdog_*` 全局量、install_modules 加 `startup_watchdog`（合并后 ~497 行 ≤500，无需裁剪）。
   - `console/static/console/scripts/operations.js`：renderOpsCenter 的 startup 状态分支与 main 的 clear_escalation 接线在不同区域，预计自动合并，冲突时手工并排保留两侧。
   - `wiki/log.md`：只追加合并（两侧条目都保留）。
4. **修复 #359 的 2 条未解决评审缺陷（带回归测试）**：
   - P2 workflow_startup.py:98：`registration_receipt_owns` 拒绝 `deduplicated=True` / 既有注册回执（`register_workflow` 返回的 `deduplicated` 标志，见 transition_gate.py:208），防止 force=True 重试把已运行工作流误标 startup_failed。测试加在 `tests/test_workflow_startup_core.py`。
   - P2 ops 卡片零任务工作流不可见（console/inventory.py:30 / `herdr/ops_center_projection.py:339`）：unscoped 请求把已注册零任务工作流并入卡片清单。测试加在 `tests/test_ops_center_read_path.py`。
5. **wiki 沉淀**：`wiki/log.md` 追加一条（只追加）；`docs/lessons/lessons-learned.md` 按现有格式追加「main 两次 force-push 回退导致已合并 PR 内容丢失」教训与合并基线检查方法（对每个已合并 PR 的 merge commit 做 `merge-base --is-ancestor` 校验）。

## 验证（真实调用链）

- 新增/受影响专项：
  `pytest -q tests/test_workflow_startup_core.py tests/test_workflow_startup_convergence.py tests/test_startup_watchdog.py tests/test_controller_startup_log_throttle.py tests/test_console_run_job.py tests/test_console_workflow_startup_visibility.py tests/test_ops_center_read_path.py tests/test_console_frontend_syntax.py tests/test_controller_modularity.py tests/test_console_ui_modules.py tests/test_wf_1029_fix1_nested_git_commit.py tests/test_wf_1029_fix6_auto_redispatch_penalize_agent.py tests/test_wf_project_1029_blockers.py`
- 面上校验：`python3 -m compileall -q herdr services bin tests`、`git diff --check`、两处行数门禁（advancement ≤500、herdr-controller ≤500）。
- 真实链路一条：`tests/test_workflow_startup_convergence.py` 走 Factory→register→preflight 失败→收敛路径，可验证合并后的启动收敛行为。

## 交付

- 推送分支并开 PR（引用 #359、附 force-push 取证与冲突解决说明）。**PR 的合并需你另行授权**，不在本次执行范围。
- 完成后建议（报告给你，不由我改设置）：给 main 开 GitHub branch protection 禁 force-push，避免再次吃掉已合并内容。