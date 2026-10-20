# Console 页面职责迁移计划

目标：入口和迁出源码均不超过 500 行，降低全局状态及函数暴露，保持现有工作流行为。
基线：207e60802c170d2d41c06d51d11676c5df6b6543；403 passed、2 skipped、82 subtests passed。
授权：本地工作树；不提交、推送、合并、部署或操作真实 Agent。

## 依赖分析

HTTP Handler → page.HTML → 品牌替换后的页面；原始 HTML_TEMPLATE 被大量 Node 行为测试消费。
服务端静态资源安装已递归同步 static/，无需新构建器或外部运行依赖。
页面顺序：tokens → 两段按顺序覆盖的 CSS → DOM → Dagre/X6 → 主脚本 → 侧栏增强脚本。
主脚本 state 是当前页面选择/后端投影，SQLite 仍为工作流状态权威；本次不复制/替换这一事实源。
仅有 8 个顶层副作用：点击、键盘、resize、10 分钟刷新、visibility、恢复视图、popstate、启动。
apiReads/pageReads、workflowExtraRequests 为请求去重；序列号和当前身份防止旧请求覆盖新视图。
opsTimer/opsCountdown、dashTimer、preflightRunId/results、日志轮询、archiveQuery 各有独立生命周期。
特别耦合：closeWorkflowTab 直接清理 logState.autoRefreshTimer；应改为调用日志模块的停止入口。
HTML 与生成标记中内联处理器依赖 window 可见的入口；renderWorkflowTabs 被后置脚本包装，必须保留兼容。

## 边界

- 服务端组合保持同一 HTML/内联脚本执行模型，避免引入异步加载、资源缓存和部署原子性变化。
- 样式按 shell、导航、表格/任务、抽屉、流程画布、模板、经验中心等组件与原覆盖层拆分；不合并/重排规则。
- JS 以完整能力为单位封装：模板浏览/编辑/图、运维视图/操作、仪表盘/选择器、项目/工作流读取、侧栏页签、任务列表/详情/轨迹、流程图/成果、控制操作/恢复/决策、经验中心、日志、归档。
- 每个能力闭包持有自身缓存/定时器/内部 helper；只导出跨模块调用或内联事件需要的函数。
- 依赖由工厂参数显式列出。页面装配只注入现有共享 state、稳定工具和跨区回调；反向 UI 回调延迟绑定，避免初始化读取尚未装配的入口。
- shared state 不顺手重建 Store；不引入事件总线、模块注册器、打包器或框架。

## 迁移顺序与验收

1. 固定基线并加入大小、组合顺序、CSS/DOM 哈希和工作流函数体等价测试；先观察大小检查失败。
2. 迁出按职责命名的 CSS，服务端组合后要求完整页面逐字一致。
3. 迁出 JS 能力闭包，保持函数体，唯一小行为等价替换为日志生命周期入口；显式依赖与有限输出；每批运行专项。
4. 更新读取源码的测试帮助器，测试最终服务页面而非仅入口文件。禁止降低已有行为断言。
5. 清理未用输出/包装；执行全部 Console 专项、独立临时 SQLite HTTP 链、Node 语法及真实浏览器验证。
6. 审查迁移前后函数覆盖、请求/确认/等待顺序、轮询停止、身份/序列隔离；更新 Wiki 导航与证据。

## 非目标

不修复既有产品缺陷，不改 API/Controller/DAG/模型调用，不更改样式、文案或交互语义。
全局桥接限于既有 HTML 事件和跨能力调用；新增能力不应再添加隐式依赖。

## 最终职责与依赖契约

| 文件（`console/static/console/scripts/`） | 负责的完整能力 / 私有数据 |
| --- | --- |
| `transport.js` | 认证请求、并发 GET 共享、页面刷新合并；两个请求 Map 私有 |
| `presentation.js` | 状态、交付阶段、时间与转义；不再反向依赖详情视图 |
| `dialogs.js` | 菜单、键盘、确认/输入弹窗与 Toast 定时器 |
| `workspace.js` / `workflow-reader.js` | 页面导航/项目水合，与选中工作流读取/轻重请求/身份序列防串线 |
| `creation.js` | 项目创建/接管/注销与异步工作流启动、等待任务回执 |
| `sidebar-tabs.js` | 侧栏分组、页签开关、折叠与渲染装饰接入口 |
| `template-library.js` / `template-editor.js` | 浏览/空间模板/DAG 与编辑/YAML/保存归属，按生命周期分界 |
| `operations.js` / `dashboard.js` | 两种独立监控视图及各自计时器；仪表盘下拉键盘位置私有 |
| `task-board.js` / `task-detail.js` | 列表/关注/交互响应，与详情/轨迹分页/轮询生命周期分界 |
| `flow-canvas.js` / `deliverables.js` | DAG/选节点/Inspector 与文件成果预览下载 |
| `controller-cockpit.js` | 控制面板、会签/纠偏、人工决策和恢复操作 |
| `shell.js` / `experience.js` | 导航与资源页，以及独立经验中心的筛选/审核 |
| `preflight.js` | 自检批次身份、结果、进度与重试 |
| `logs.js` / `archive.js` | 日志读取/导出/轮询，与归档查询/分页；数据不再向页签模块暴露 |
| `bootstrap.js` / `shell-enhancements.js` | 启动/历史/刷新监听，与侧栏尺寸/页签滑动指示器 |

工厂参数定义输入，返回列表定义可调用能力。稳定底层依赖直接传入；反向 UI 回调延迟绑定，防止装配前取到 undefined，并保留后置页签装饰。`workspace.js` 保留显式的视图协调职责；工作流读取单独由 `workflow-reader.js` 管理；没有通过 Context/Proxy/事件总线隐藏这一扇出，也没有声称 UI 回调图无环。

共享 `state` 仍代表当前页面选中项及后端投影。这一轮隔离能力私有数据和 helper，不重造 Store 或改变工作流状态权威来源。新增实现应修改所属能力的私有逻辑；跨能力变更需显式修改输入或返回列表。

CSS 位于 `console/styles/`：基础、侧栏、控件、进度、选择器、任务列表、对话框/抽屉、运维、shell 布局/导航/工作区/Inspector、模板、经验中心及原有覆盖层。覆盖层保留原顺序，不以重排规则换取看似更干净的组件样式。

## 清理与行为差分

- 删除未使用的 `submitNewWorkflow` 别名；保留原使用的 `submitNewWorkflowAsync`。
- 两行共享状态回归组合入口，未保留无封装意义的 state 文件。
- 删除生成的重复模块契约 JSON；源码参数/返回列表是唯一可执行契约。
- 日志停止从页签模块直接操作 timer，变为日志能力入口。停止时机和次数由完整脚本测试证明。
- 发现并修复闭包绕过 `window.renderWorkflowTabs` 装饰器的迁移回归。新增失败测试后引入 `setWorkflowTabsRenderer`，只接入原装饰，不改变原渲染函数体。
- 原 326 个函数体在实际装配、闭包内部读取 `Function.prototype.toString()` 对照固定基线；仅 `closeWorkflowTab` 的日志停止调用进行明确等价归一化。CSS 两块与完整 DOM 骨架保留原哈希。
- 每个迁出 JS 是能力闭包，内部 helper/缓存/计时器不可从页面命名空间直接访问。全局绑定 350 → 235；Acorn + eslint-scope 本地分析未发现新增未解析引用或绕过输入参数的旧同级符号引用。

## 验收边界

Console 专项包含真实 HTTP → Kernel / StateStore → 临时 SQLite → HTTP 读取。浏览器用 `tests/support/console_ui_fixture.py`，仅替换物理终端清单和健康探针，不启动 Agent、不调用模型、不访问生产数据库。

这是本地工作树交付。全量 CI、真实 Agent 派发执行和生产部署不在本次验收范围。Google 标准自审单独记录，不能当作独立评审；当前没有 health / codehealth / interrogate 工具，结构检查用明确的基线差分和作用域分析补证。

静态直访兼容：`Handler.send_static` 对原有 `/static/console.html` 返回同一已组合 `HTML_TEMPLATE`，保留旧路径可执行性；其他静态文件保持原读取方式。

## 本地交付证据

- 入口 421 行，23 个职责脚本与 19 个样式资源；所有迁出源文件不超过 500 行，最大 495 行。输入/输出声明展开后仍满足上限，没有压缩原逻辑来隐藏行数。
- 最终 Console 专项：409 passed、2 skipped、82 subtests passed。两个跳过项为临时 HOME 中未安装的 LaunchAgent 检查。
- 语法/编译、F821/F822/F823、diff 检查通过；全局绑定从 350 降至 235，没有新增未解析名字或绕过参数的同级引用。
- 最终浏览器真实点击验证暂停/恢复、持久改名、跨工作流切换、DAG/滑动指示器及日志轮询停止；HTTP 与独立只读 SQLite 查询一致，旁侧工作流保持 running。交互期间未捕获页面异常。
- 原 DOM/CSS 哈希和 326 个函数对象基线通过。后端调度和业务 API 实现没有修改；HTTP 唯一兼容改动为旧静态页面路径组合。
- 生产源码聚合 SHA256：`ba08e667ff904dd8253fd46a0178e30d1f74ea2e7b88981eca54ae22980b2560`。
- 本地自审未发现范围内阻塞缺陷；独立评审、全量 CI、真实 Agent 执行与生产部署尚未进行。初次本地交付未提交、推送或创建 PR；用户随后已授权创建 PR。

PR 前独立评审发现导航/工作流读取模块重复装配；已删除重复 include，新增全部资源恰好装配一次的验收，并让函数对象基线记录器遇到重复定义即失败。
