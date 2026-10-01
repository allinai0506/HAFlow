# Console Shell v1 代码验收与收尾

2026-09-30 恢复 `ui-upgrade`。用户确认仅完成代码验收和收尾记录。本轮未修改功能源码或测试，未部署、重启、删除分支或 Clone；当时本记录仅保存本地，未提交或推送；2026-10-01 按用户授权补交归档。

## 合并证据

- [PR #116](https://github.com/allinai0506/HAFlow/pull/116) 状态 MERGED，合并时间 2026-09-30T00:09:54Z。
- 功能提交 `c21c6abcb42214dfa33d5de5d15a82a919e93cd6`；合并提交 `dac0fd8908dec0b0b1a6fb526364ab8f8ecd0696`。
- `git fetch origin` 和 `git merge-base --is-ancestor HEAD origin/main` 均 exit 0。
- 目标与实现范围见 `docs/specs/console-shell-v1.md`、`docs/superpowers/plans/2026-09-29-console-shell-v1.md`。

## 本轮测试

结果绑定上述功能提交，测试后未修改源码或测试。

- `/opt/homebrew/bin/pytest -q tests/test_console_shell.py tests/test_console_flow_workbench.py tests/test_console_templates.py tests/test_console_project_creation.py tests/test_console_run_job.py tests/test_console_dashboard_api.py tests/test_console_frontend_syntax.py tests/test_console_view_state.py`：exit 0，101 passed，1.33s。
- `/opt/homebrew/bin/pytest -q`：exit 0，2384 passed、50 subtests passed，309.53s；0 failed、0 skipped、0 xfailed。
- `python3 -m compileall -q console herdr services bin tests`：exit 0。
- `git diff --check`：exit 0。

## 验收边界

- 历史独立评审与浏览器证据见 `.omc/review-01a0ed49-6ed4-7770-bc40-9f8568d8817a.md` 及同编号 verify 文件。本轮未重新独立评审或执行浏览器视觉、交互验收。
- `http://127.0.0.1:8765/` 返回 HTTP 200，但响应与当前源码 `HTML.encode()` 不一致，不能作为本提交部署验收证据。历史预览端口 8771 本轮返回 RemoteDisconnected。
- `./bin/herdr-task verify-baseline ui-upgrade` 返回 `Task not found: ui-upgrade`，未取得 CoW Task 基线验收证明；Git 核查不替代此证明。
- 未触发真实 Agent、收费模型或运维写操作。保留现有分支和运行现场。
- 本轮仅补交付记录，未改变宏观行为；依据 `wiki/WIKI.md` 不新增 Wiki 页面或重复工程教训。

代码已合并，本轮回归通过；部署及真实浏览器验收未完成，按用户选择留在本轮范围外。

## 当时的后续授权：本机热更新

用户随后明确要求“热更新”，覆盖前述本輪不部署的范围。已执行 `./scripts/install-herdr-console.sh`，exit 0；仅重启 `com.user.herdr-factory-console`，新 PID 15106。LaunchAgent 的 LastExitStatus 15 是被 kickstart 替换的旧进程退出记录，当前服务响应正常。

- 回滚副本：`/Users/user/.herdr-console-backups/20260930-095622`，包含更新前部署目录；需要回滚时恢复该目录内容并 kickstart 控制台。
- 部署脚本字节与当前提交源码一致；HTTP 200 页面逐字节等于源码 HTML，SHA256 `244ec4464f6cbed093e29e1f395f1e18f5fde3537dd861fe2cd88dff467d3c3d`。
- `/static/` 下全部五个文件逐字节匹配仓库资源，包括 X6 和 Dagre。
- Chrome 在实际 8765 服务上显示分组导航、6 个真实 DAG 节点及右侧“读作”检查器；任务列表 → 告警 → 工作台恢复 9 个任务行，再切回流程图正常。
- 未点击推进、重派或豁免按钮，未改变任务执行状态。此次只做上述浏览器冒烟，不声称所有导航或状态生命周期已完成 E2E。

## 归档说明（2026-10-01）

本文保存 2026-09-30 当轮验收和热更新结果，测试数字、PID、部署摘要及备份路径均为历史证据，不表示当前运行版本。本次仅核对 PR #116 的 MERGED 状态与合并提交；没有重新执行本文的历史测试或浏览器验收。

上述恢复部署目录并 kickstart 的操作记录适用于当时部署方式。当前 Console 已使用 `~/.herdr-controller/releases/<commit>` 冻结快照；维护或回滚前必须核对实际 LaunchAgent 配置，按 [服务管理指南](../operations/service-management.md)更新快照路径并重载对应服务。本文不是当前版本的回滚操作手册。
