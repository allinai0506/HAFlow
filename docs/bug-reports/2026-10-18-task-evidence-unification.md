# 执行轨迹内容与任务视图一致性

基线：77e4f0c8ae59d4c6c63cf4fe7f71d1da7f7faf62。
隔离目录：`/Users/user/.sandboxes/haflow-task-evidence-1018`。
分支：`fix/task-evidence-unification`；交付范围为工作区修改，未提交、Push、合并、部署或重启服务。

## 目标与边界

让任务轨迹回答派发目标、执行进展和产出；流程图侧栏与列表展示同一任务内容。
复用 SQLite 事件、任务记录、产物目录与现有原生适配，不新增存储、依赖或调度语义。
验收包含真实只读调用链、同 Run 隔离、脱敏、报告不冒充验收、两种视图与菜单的一致行为。

## 根因

`read_stored_events` 的事件类型白名单未包含 `task_artifact_checkpoint`、候选版本与工具执行回执；Qoder 没有原生工具适配。真实洁净室任务已存有 174 项测试与边界核验摘要，接口却只投影状态。

Inspector 独立拼接简略任务行，且过滤已替代任务，列表则保留这些记录。统一渲染时还须消除菜单的全局 ID 定位，否则会打开隐藏列表中的菜单。

## 修复与防复发

- 轨迹读取已有阶段报告、产物回执、候选 SHA 与工具回执；派发展示执行者和任务目标。
- 摘要复用 `redact_text`，不读取原始提示词、完成凭据或任意产物内容。报告保持中性，超时及副作用未知照实展示。
- 轨迹页复用既有产物渲染；有具体报告或操作时折叠无详情的状态记录。
- `renderTaskRow` 供列表、Inspector 任务页与概览使用；菜单定位当前点击元素。已替代任务可查，节点聚合规则不变。
- 新测试证明上述 API/只读/脱敏/Run 隔离及渲染、菜单行为。

## 验证与回归

RED：新增真实临时 SQLite API 测试未读到派发目标与报告；两视图一致性测试缺少已替代任务。菜单回归先因全局 ID 定位失败。

GREEN（最终源码，2026-10-18）：

```bash
/opt/homebrew/bin/python3.13 -m pytest -q tests/test_execution_trace.py tests/test_execution_trace_api.py tests/test_console_execution_trace.py tests/test_console_flow_workbench.py tests/test_console_task_detail.py tests/test_console_frontend_syntax.py tests/test_workflow_graph_projection.py tests/test_deliverables.py
```

结果：161 passed, 3 subtests passed，退出码 0。包含已有真实 HTTP 与真实原生会话只读链验证。

`ruff check --select E9,F63,F7,F82` 对三个源码及两个改动测试通过；`compileall` 与 `git diff --check` 通过。完整 Ruff 存在基线风格债，未扩大范围清理；按诊断 code/message 与基线比较确认未新增诊断。

Ego Browser 用真实任务 `wf-haflow-1018-01-cleanroom_test-auto` 的只读投影和生产渲染函数做浏览器验收：两视图内容相同，侧栏菜单打开而列表菜单关闭，状态区折叠。该页面是隔离验收页，未替代真实部署后 UAT。截图：`/tmp/haflow-evidence-1018/browser-final.png`。

自审：检查了事件读取与 Run 过滤、公开字段、脱敏、报告语义、任务行及菜单调用链；未执行独立评审或 CI 全量。

## 已知限制

Qoder 历史逐条工具调用未被适配；无记录时不能恢复具体 shell/MCP 操作。页面明确说明，只展示已保存报告和回执。执行者报告中“通过”不代表本次重新验收。未部署，线上页面尚未改变。通用教训沿用现有轨迹归属与证据约束，本次同步对应 Wiki，未新增重复教训。

## 候选检查跟进：历史产物读取

用户检查 `e7fb86e` 时发现报告不可查看、下载。真实报告仍在受管工作区，根因是 `_file_item` 对已替代任务强制设置 unavailable/readable=False。改为 historical（历史产物）并允许既有查看、下载链路；历史数量独立统计，原有节点预期排除规则保留。历史 PR 可打开，提交记录仍不伪装成可下载文件。

新增 RED→GREEN 覆盖任务 status/superseded_by 两种替代方式、归档历史文件、缺失文件、跨任务/工作流拒绝、真实 HTTP 预览和下载；复用全部路径、敏感文件及归档校验。此次是用户明确要求的历史查阅语义修正，旧“已替代即不可读”断言由更完整的历史读取与不计入当前交付断言替换。

历史产物跟进最终验证：受影响 10 个测试文件执行 203 passed, 3 subtests passed（退出码 0）；Ruff correctness、compileall、diff check 均通过。真实报告读取链预览 6,172 字符，下载 8,907 字节，SHA-256 `36b27caedab320d3904fb4c33ea031ee5e31c8686b4fd43081ce22a6fa564959`。安全自审保留 workflow/task 归属、受管路径、后缀/敏感文件、符号链接与归档校验；没有新增端点、存储、权限或外部依赖。仅用户要求的历史可读语义变化；提交不是文件，工位内部自检卡片仍为元数据。

## 检查跟进：轨迹信息被挤出首屏

在线运行 5295ca6，包含 e7fb86e 全部修改；两次前端差异只有历史产物计数。真实浏览器确认原任务仍有派发目标和两份报告，流程图测试节点的 5 行任务文本全部与列表对应行一致。

实际展示回归：完整属性卡、产物和内部自检卡片排在轨迹事件前，具体执行报告被挤出首屏。最小修复为轨迹先事件后产出，并省略其他页签已有的重复完整属性卡。新增回归测试先失败于报告顺序，覆盖报告优先、属性去重与产物保留；不改事件采集、状态、下载权限与共享任务行。

轨迹优先修复验证：204 passed, 3 subtests passed（退出码 0）；Ruff correctness、compileall、diff check 均通过。

## 检查跟进：同一工作流画布不刷新

用户明确问题集中于流程图和列表不一致。发现 `renderFlowGraph` 只检查 workflow ID；同 workflow 更新直接调用只处理 selected 的 `updateFlowSelection` 后返回，列表读取最新任务，画布却保留旧节点和连线。

新增 RED→GREEN 测试复现 working/claude/1 快照更新到 completed/qodercli/2 后画布仍为 working；修复将 graph projection 纳入复用条件。投影未变仍复用，变化则按既有构建逻辑重建节点和连线，不引入第二套任务状态。真实 X6/Dagre 隔离浏览器点击更新验证了这些字段及下游 waiting→working 的同步。77 passed, 3 subtests passed；Ruff correctness/diff check 通过。尚不能把此缺陷等同于用户所有展示差异；正在等待用户明确画布卡片或侧栏任务页。

真实用户页面另复现侧栏排版缺陷：`#flowInspectorBody` 为 flex 容器，任务行默认 flex-shrink:1，被压到 66.5px；内容 grid 第一行 61.6px、第二行 0px，执行者溢出到下一条任务。最小修复为侧栏任务行 flex-shrink:0。77 项专项及 3 项子测试、Ruff correctness、diff check 通过；部署后以真实 DOM 行边界核验。

## 用户截图确认：两处单任务详情必须一致

用户最终明确对比的是流程图节点详情与列表任务详情，不是任务行或刷新。Git 检索显示本轮 e7fb86e 只抽出了共享任务行；现存 `selectFlowNode` 始终走旧 Node Inspector，未调用统一任务详情。这是本轮实现误解，不能声称完整详情已统一。

最小修复：有任务的画布节点调用既有 `openTaskDrawer`，与列表使用同一容器、同一 API 装配和四页签。默认最后一个未替代任务，无当前任务则最后一个历史任务；无任务节点保留定义概览。删除 HTML 层重复 click 监听，保留 X6 node:click，避免双请求。无新增详情状态、API、模型或存储。

RED→GREEN：画布/列表入口同 task_id 测试原来失败于画布未调用任务详情；修复后 176 passed, 3 subtests passed，含图、详情、轨迹、产物及 frontend syntax 专项。Ruff correctness、compileall、diff check 均通过。待部署后真实双入口四页签对比。
