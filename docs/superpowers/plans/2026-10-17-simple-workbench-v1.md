# 极简工作台 V1 实施与验收计划

目标：按用户列出的 12 项验收提供 /simple，保留专业工作台。单工位串行；提交本地代码，不 push、PR、合并、部署或重启生产。

基线：986ae4d08f22cba473537db0f304f74950f08cd7；已 fetch；verify-baseline 初始 BASELINE_MATCH。任务描述中的未提交实现不在本克隆，按现有 API 完成。

设计：原生 HTML/CSS/JS 独立静态资产，由现有 Handler 服务。复用项目注册表、SQLite workflow/task、模板目录、共享决策账本及 deliverables。URL 为页面上下文，localStorage 仅保存选择与未提交草稿，不保存业务状态。专业入口保留 /，双向链接携带 project_id/workflow_id。

信任本机操作员及既有 API 权限边界；文本用 textContent/HTML 转义，文件读取仅用既有成果 ID 接口。测试状态隔离；不启动收费模型或真实 Agent。外部终端/模型允许受控替代，但项目持久化、模板、workflow/task、决策、文件读取必须真实。

- [x] RED：HTTP 页面与项目列表、非 Git 注册、模板详情、启动上下文测试。
- [x] GREEN：/simple 静态资源及项目列表 API；create_project 按执行模式支持 context 普通目录；提供业务 context 模板。Git 模板在普通目录明确报错。
- [x] UI：三个操作、全部分类和实际模板节点/context 输入、异步 job 轮询并精确进入 WORKFLOW_ID；状态/决策/成果；失败重试和响应式键盘操作。
- [x] 验证：受影响 console/projects/templates 测试、真实 HTTP/SQLite/文件集成；真实浏览器桌面与窄屏；旧 / 冒烟。
- [x] 清理自审、文档/Wiki、loop eval、verify-baseline。
- 本地提交、最终受管报告与完成声明按平台回执记录（不等于业务验收/合并/部署）。

验收映射：1/12 页面与浏览器；2/3 持久化项目 HTTP；4/5 模板真实 API/浏览器；6 真实 run job/状态；7 决策账本 HTTP 往返；8 文件预览/下载及归属反例；9 双向 URL；10 空状态/失败；11 旧 console 专项。

未知边界：完整真实 Agent 执行不可在测试中隐式启动；生产运行冒烟不属于本任务部署权限。记录验证覆盖，不能将受控测试称为生产已验收。

清理计划与结果：仅检查本次改动；删除 JS 中只写不读的 detail/workflows 缓存，保留模板与 UI 选择所必需的状态。未引入依赖、抽象层或新状态机。失败接口、预览、下载使用原有边界；新增 Python 测试的导入已排序。自审为单工位自审，不冒充独立评审。目录归一化导致导航旧需求的通用教训已按仓库 knowledge-capture 技能追加至 lessons §166；新装状态库边界保留在专项回归。
