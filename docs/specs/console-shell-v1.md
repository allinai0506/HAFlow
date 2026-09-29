# Console Shell v1

视觉契约：`/Users/user/Desktop/haflow-flow-canvas-proposal.html`。

Route: resume=fresh, intent=feature, complexity=medium, risk=[], domain=[ui],
delivery_target=pull_request, spec_backend=unified, controller=none,
proof_mode=acceptance-test + visual, assurance=deterministic.

## 1. 目标 / 非目标

目标：控制台主壳与提案页一致。左侧是产品导航，工厂空间是顶部切换器。工作台顶栏只留当前工作流的面包屑和「进入下一阶段 / 新需求 / ···」。流程图节点是类型、状态胶囊、标题、目的、计数。右侧概览是「读作 + 现状 + 当前任务」。

非目标：不改图投影、调度、路由。不加节点库、版本对比、试运行、七日运行图。不新增接口。

## 2. 信息架构

- 切换器：当前空间名称 + 关系文案。菜单内列出已有空间，并保留「新建工厂空间」。
- 运转：工作台、仪表板、运维驾驶舱、告警。
- 当前空间：工作流（活跃数）、执行者（活跃数）、工位。
- 治理：Controller、模板库、归档。入口调用现有函数。
- 计数只在大于 0 时出现。

## 3. 工作台

- 面包屑：`分区 / 工作流标题`，副标题沿用现有 workflowSub。
- 统计条、阶段条、底部「执行者阵容」在工作台不显示。统计节点仍在 DOM 中供原有脚本写入。
- 画布工具条：流程图 | 任务列表，节点摘要，+ − Fit。任务筛选只在任务列表出现。
- 节点用 X6 HTML shape `flow-card`。标题用 `cleanStageLabel`。无任务时底行写「尚未开始」。
- 概览用节点 purpose、执行者、上下游和真实任务。解卡仍走 `executeControllerAction`。任务行仍走 `openTaskDrawer`。

## 4. 验收

- 静态 HTML 含 spaceSwitcher、crumbSection、data-nav="workbench"、新建工厂空间、>模板库</button>、flow-card、读作。
- 既有 flow workbench DOM 钩子、dashButton、syncOpsUi 双向文案仍在。
- 浏览器中工作台布局与提案同构：228px 侧栏、分组导航、点阵画布、300px 检查器。
