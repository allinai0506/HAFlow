# 流程图与列表统一任务详情、补全执行证据及历史产物读取

## 范围与状态

基线：`77e4f0c8ae59d4c6c63cf4fe7f71d1da7f7faf62`。
分支：`fix/task-evidence-unification`。
隔离目录：`/Users/user/.sandboxes/haflow-task-evidence-1018`。

业务源码已作为候选 `ecb638e3719e13773fefdc6b642715640ae6456f` 部署到本机 Console，运行指纹与源文件已核对。后续交付整理仅更新文档；未合并主干、未重启 Controller。部署回滚回执位于 `~/.herdr-controller/deployments/shared-detail-ecb638e3719e/receipt.json`，保留旧快照与 Console plist。

## 目标

- 流程图点击已有任务的节点，与列表点击同一任务，打开相同详情和四个页签。
- 执行轨迹展示可归属的派发目标、执行报告、候选提交和产物回执。
- 已替代任务的历史文件可以查看、下载，但不能满足当前节点交付要求。

复用现有任务记录、SQLite 事件、原生适配、任务抽屉和产物读取接口；不新增存储、依赖、模型调用或调度语义。

## 根因与修复

1. 画布 `selectFlowNode` 只打开旧 Node Inspector，列表走 `openTaskDrawer`。先前仅统一任务行不能满足详情一致性。现在两入口均调用 `openTaskDrawer(task_id)`，复用同一容器、API 和「概览 / 执行轨迹 / 产物 / 运行时」。默认最后一条未替代任务，全部已替代则选最后一条历史任务；历史记录仍可在节点任务页选择，无任务节点保留定义概览。删除 HTML 层重复 click 监听，保留 X6 node:click，避免双请求。
2. 轨迹事件白名单未包含阶段报告、候选版本和工具执行回执。现在投影已有事实，并复用脱敏；执行者报告保持中性，不能替代验收。具体事件放在重复状态及产物卡片前，轨迹页省略概览/运行时已有的完整属性卡。
3. 历史文件因任务已替代被直接禁止读取。现在标记 `historical`，沿既有接口查看、下载，独立计数；原有当前节点预期排除规则、归属、路径、敏感文件、符号链接、归档与大小限制均保留。提交记录仍不是可下载文件，历史 PR 链接可以打开。
4. 画布只按 workflow ID 复用，数据更新后列表变而节点和连线不变。现在把 graph projection 纳入复用条件，未变复用、变化重建。
5. 侧栏任务行被 flex 容器压缩，执行者文字侵入下一行。侧栏行禁止压缩；共享行菜单改为定位当前点击元素，避免全局重复 ID 打开隐藏列表的菜单。

## 验证与回归

RED→GREEN 用例覆盖：同任务双入口、画布数据变化/不变、历史任务/归档文件可读、历史文件不满足当前节点预期、跨任务/工作流拒绝、文件缺失、报告中性与脱敏、报告优先和菜单定位。

最终源码专项：

```bash
/opt/homebrew/bin/python3.13 -m pytest -q tests/test_console_execution_trace.py tests/test_console_task_detail.py tests/test_console_flow_workbench.py tests/test_console_frontend_syntax.py tests/test_console_workflow_latency.py tests/test_workflow_graph_projection.py tests/test_execution_trace.py tests/test_execution_trace_api.py tests/test_deliverables.py tests/test_workflow_clone_reclaim.py tests/test_deliverable_read_latency.py
```

结果：`216 passed, 3 subtests passed`，退出码 0。包含真实临时 SQLite、HTTP 预览/下载、已有原生会话只读链与归档校验。修改文件的 Ruff correctness（E9/F63/F7/F82）、compileall、diff check 均通过；完整 Ruff 的基线风格债未扩大范围清理。

真实浏览器（用户当前标签页）从画布“代码工坊”和列表 `haflow-issue-255-agy-trust-r2` 分别打开详情；任务 ID 相同，四页签完整可见文本逐项相等。历史报告 `2026-10-18-cleanroom-test-f76c630.md` 已实际预览并下载 8,907 字节，SHA-256 `36b27caedab320d3904fb4c33ea031ee5e31c8686b4fd43081ce22a6fa564959`。侧栏 5 行执行者均在各自行边界内。隔离页面使用真实 X6/Dagre 验证同 workflow 的状态、执行者、任务数量和连线同步，不修改生产任务状态。

独立规范审查和需求审查未发现阻塞缺陷；规范审查额外独立运行 56 项测试通过。CI 全量由 PR 合并结果执行，不能用本地专项替代。用户业务验收与合并仍独立于这些验证。

## 已知限制与范围外

- Qoder 历史逐条 shell/MCP 工具回放仍无原生适配；页面明确说明，只显示已保存的报告和回执，不伪造工具事实。
- 执行者报告中的“通过”不是本次重新验收；工位内部自检卡片仍为元数据。
- 本次不改变节点完成条件、调度、资源回收、数据库 schema 或访问权限。
