# 缺陷复盘报告: wf-haflow-1024-02-craft-rework-manual

- 任务标识: wf-haflow-1024-02-craft-rework-manual
- 分支名称: agent/codex/fix-wf-haflow-1024-02-craft-rework-manual
- 执行者: codex
- 生成日期: 2026-10-25

## 根因

`polish-shell.css` 已把 `.drawer-head` 与 collapsed 可见高度提升为 48px，但真正定位 fixed 抽屉的 `shell-inspector.css` 仍在桌面和窄屏规则中写死 `bottom: 28px`，底栏本身也只有 28px。折叠 transform 留出的 48px 头部因此位于底栏上方并侵入主工作区。

第一版返工假设曾把 `bottom` 同步为 48px；真实浏览器立即证伪：fixed 元素的 `bottom` 是整个抽屉离视口底部的距离，这会把抽屉头继续向上推。正确几何是 `.light-bottom-bar height: 48px`、`.drawer-head height: 48px`、`.deep-drawer bottom: 0px`，让折叠头占用底栏带而非工作区。

## 防复发

- `tests/test_console_cockpit_e2e_harness.py` 按选择器规则块解析 CSS，分别锁住底栏/抽屉头 48px 一致性及桌面/窄屏两条 `bottom: 0px`；不再用整文件子串断言。
- 反证：临时还原为旧值后，两条新护栏分别以 `[28, 28] != [0, 0]`、`28 != 48` 转红；恢复实现后转绿。
- `tests/test_console_controller_cockpit_e2e.py` 启动临时 HTTP 服务和真实 ego-browser，采集包围盒与 `elementFromPoint`，执行真实 pointer click，并验证进入 Controller 且抽屉未误开。
- `tests/test_console_module_boundaries.py` 通过 fixture 同时校验两个合成 style 块；`tests/fixtures/console-ui-baseline.json` 的 `styles[0]` 按实际合成结果更新为 `05c3892ac33b4f1cc6b399440607df038f440f4a0be7c473ea553026f1ba7feb`。
- 通用教训已追加到 `docs/lessons/lessons-learned.md` §199，并同步 `wiki/log.md`。

## 验证与回归

### RED → GREEN → 反证

- 初始 RED：`pytest -q tests/test_console_cockpit_e2e_harness.py -k 'collapsed_deep_drawer_is_anchored_below_the_workspace or light_bottom_bar_rule_matches_drawer_head_height'` → 2 failed，实测 `[28,28] != [48,48]`、`28 != 48`。
- 正确几何的二次 RED：`bottom: 48px` 被浏览器证伪后，将期望收紧为 `[0,0]`，当前实现以 `[48,48] != [0,0]` 失败。
- 最终 GREEN：Console 受影响专项 `46 passed in 32.79s`。
- 撤销必红：临时恢复旧 `height/bottom: 28px` 后 scoped 护栏 2 failed；随后恢复最终实现并重跑通过。

### 真实浏览器证据

1440x900 视口：

- “在 Controller 中处理”按钮 bbox：`x=1170, y=832.515625, width=125.359375, height=26.3984375`；中心 `(1232.6796875, 845.71484375)`。
- 抽屉头 bbox：`x=268, y=853, width=1172, height=48`；底栏 bbox：`x=268, y=852, width=1172, height=48`。
- `document.elementFromPoint(1232.6796875, 845.71484375)` 返回 `BUTTON`，文本为“在 Controller 中处理”，`hitIsButton=true`。
- `page.click(...)` 后状态为 `workflowId="__ctl__"`、`nodeId="gate"`，且 `drawerCollapsed=true`。

### 全量与静态检查

- `pytest -q` → `6476 passed, 5 skipped, 5 warnings, 371 subtests passed in 1957.96s`，0 failed。
- `python3 -m compileall -q herdr services bin tests console` → exit 0。
- `git diff --check` → exit 0。
- `ruff check --output-format json .` → exit 1，仓库既有 6975 项 lint 基线；本次内循环报告为 `baseline 6975, current 6974, new 0`，未扩散 lint 债务。
- `/Users/user/HAFlow/bin/herdr-loop eval` 连续两次输出 `score=100.0`，但同时输出 `converged=false`；`EVALUATION.md` 显示 collect-only 将 4 个含 `✓/√ Tests ... passed` 的参数化测试标题误列为失败，同时明确 `new lint=0`、范围合规。该解析矛盾未作为测试 PASS 冒充，仓库真实全量结果以上述 `pytest -q` 为准。
