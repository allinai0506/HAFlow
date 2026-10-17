# 缺陷复盘报告: impl-trace-core-1016-r3

- 任务标识: impl-trace-core-1016-r3
- 分支名称: agent/qodercli/fix-impl-trace-core-1016-r3
- 执行者: qodercli
- 生成日期: 2026-10-17

## 根因
F4（评审唯一阻塞项）：任务抽屉执行轨迹的 8s 轮询以 `state.selectedTaskDetail` 判定任务是否
working，而该快照仅在 `openTaskDrawer` 打开抽屉时从 `/api/task` 写入一次，全链路无任何刷新路径
（`renderTaskDrawer` 只读快照、`refreshAll` 不触碰）。判定源是打开瞬间的冻结事实，不是任务当前
状态，导致停止条件在生产链路不可达：

1. 任务完成（completed/cleaned 等）后，只要抽屉保持打开且打开时为 working，`traceTaskWorking()`
   恒为 true，`/api/task/trajectory` 每 8s 持续轮询（单次最长 4s 原生会话扫描，MAX_SCAN=800/
   单文件 8MB），无界消耗控制器资源；
2. 反向：打开抽屉时任务 pending/blocked、之后转 working，因启动条件同样读取冻结快照，轮询永不
   启动，执行中任务看不到自动刷新的轨迹。

深层根因：把"取态一次"当"状态订阅"。既有 node 测试通过手工改写内存 status 验证守卫，与被测
生产链路（fetch → 状态取态 → 轮询决策）脱节，掩盖了判定源失效。

同分支承载的 DEFECT-5/P1：`.td-ev-icon{margin-right:5px}` 由候选 4e543ee 引入，违反
4/8/12/16px 间距 token，使守卫 `test_no_spacing_grid_violations` 由绿转红。

## 防复发
1. 轮询/守卫类逻辑的测试必须走真实调用链：mock 只允许替换网络边界（fetch），禁止手工改写内存
   状态验证守卫（AGENTS.md §4.8/§4.10）；本轮已把旧的手工改写式测试替换为 fetch 边界驱动的
   双向回归（完成→停止、pending→working→启动）+ document.hidden 守卫 + 非 trace 页不轮询。
2. 派生自服务端状态的 UI 决策，若存在轮询周期，必须在回调内重新取态，不得依赖打开时快照；
   停表终态集合与服务端 `TERMINAL_LIKE_STATUSES`（services/herdr-controller.py:10316）对齐，
   避免两侧各自维护枚举漂移。
3. 新增 CSS 间距必须使用 token 值；`tests/test_console_standard_layout_tabs.py` 的间距守卫作为
   CI 门禁持续兜底。

## 验证与回归
- RED（修复前行为复现，仅驱动旧函数）：后端报 completed 后 3 次 tick 仍发出 3 次 trajectory
  请求、0 次停表；打开时 pending 建立定时器数 0。新回归测试在旧代码上失败于缺失
  `refreshSelectedTaskStatus`（生产链路无刷新路径）。
- GREEN：`pytest tests/test_console_execution_trace.py` → 6 passed。
- 受影响专项：`pytest tests/test_console_execution_trace.py tests/test_execution_trace.py
  tests/test_execution_trace_api.py tests/test_trajectory_observer.py tests/test_trajectory.py`
  → 161 passed。
- DEFECT-5 守卫：`test_no_spacing_grid_violations` → 1 passed；整文件 15 passed。
- Console 影响面：`pytest tests/test_console_*.py` → 309 passed, 76 subtests passed。
- 前端语法：`pytest tests/test_console_frontend_syntax.py` → 16 passed。
- 全量本地套件（1 次）：1 failed / 4597 passed, 174 subtests —— 唯一失败即运行中已修复的
  DEFECT-5 守卫；全量门禁由 CI 在合并结果上执行（AGENTS.md §6.4）。
- `python3 -m compileall -q herdr services bin tests` 通过；`git diff --check` 干净；
  ruff 与基线持平（new 0）。
- 未验证项：真实浏览器内抽屉轮询行为（测试以 fetch 边界+定时器驱动替代，未部署/未启动真实
  Console 服务，遵守工位副作用禁令）。
