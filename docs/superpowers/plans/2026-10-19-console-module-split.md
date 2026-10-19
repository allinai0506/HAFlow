# Console Module Split Implementation Plan

> **For agentic workers:** 执行记录已完成。delivery_target=working_tree，禁止 commit/push/PR/部署。

**Goal:** 按职责拆分 Console 单文件，保留真实调用链和外部行为。

**Architecture:** common 提供路径/进程基础能力；inventory 提供基础状态读取和工作流投影；runtime 提供终端/工位现场读取；read_api 聚合详情/交付/看板；workflow_api 处理模板/启动/恢复；task_api 处理交互；control_api 处理内核和控制器操作；http_server 保留 HTTP envelope、鉴权与路由；入口只装配启动及显式兼容导出。依赖方向仅由外层指向内层。

**Tech Stack:** Python 标准库、现有 herdr 核心、pytest、Node。

## Global Constraints
保持规格中的 INV-HTTP/PAGE/IMPORT/STATE/SCOPE/STRUCTURE；无新依赖或产品行为；只在当前 CoW 内修改。页面约 10500 行整体作为资源移动，避免拆分前端加载时序。新模块只是 Console 装配，不替代 herdr 中的纯核心。

## Task 1：锁定基线和边界
- [x] 运行 `.omc/console-split-tests.txt` 中所有入口调用方专项，区分基线失败与新增回归。
- [x] 将原 HTML_TEMPLATE 的 SHA256 和全部函数 AST 保存在本地证据，作为行为保持比较。
- [x] 新增 `tests/test_console_module_boundaries.py`，覆盖真实临时 SQLite HTTP 写读、静态资源拒绝越界、跨 cwd 子进程启动与模板完整性；在旧实现先运行适用用例。

## Task 2：按职责搬迁并校正测试依赖所有者
- [x] 创建 Entry Gate 列出的八个模块和模板文件；采用 `from console import inventory` 等普通模块依赖，跨模块调用写明所有者。
- [x] 基础读取和投影归 inventory；详情/轨迹/交付/看板归 read_api；模板/启动/恢复归 workflow_api；交互归 task_api；内核/控制命令归 control_api；HTTP 与 Hub 认证传输归 http_server。
- [x] 保持原函数体和常量值，仅替换全局引用的模块限定名；逐函数 AST 去限定名后与基线比较，排除新启动装配。
- [x] 原入口显式导出旧符号；SourceFileLoader 仍可读 HTML/Handler/API；测试的 patch.object/monkeypatch.setattr 指向真实所有者，动态 patch 循环手工核查。
- [x] 模板以 `read_text(encoding='utf-8')` 装载，渲染后字节与基线一致；不引入新前端请求或改变缓存策略。
- [x] 验证普通包导入、直接脚本启动和服务快照路径；原 __main__ 的 deliverable enrichment 初始化迁到明确启动位置，避免丢失默认 loader。

## Task 3：清理、验证与评审
- [x] 使用 ai-slop-cleaner Mode B 删除拆分后未用导入、重复常量和临时兼容样板；不改动无关既有行为。
- [x] 运行专项 pytest、`ruff check --select F821,F822,F823 console`、`python3.13 -m compileall -q console`、`git diff --check`；HTTP 测试用临时数据库和只属于测试的服务线程，禁写生产。
- [x] 对基线与后验记录 Python 文件规模、依赖图及测试/静态检查，不以降低测试断言换取通过。
- [x] 更新 Console README 的已过时运行副本描述，以及根导航、相关 Wiki 和只追加日志。
- [x] 按 google-code-review 进行独立只读评审，提供规格、完整 diff 和新鲜证据；有效缺陷回 S4→S5→S6，最多三轮。
- [x] 写 `.omc/verify-console-split-20261019.md` 和 `.omc/review-console-split-20261019.md`，本地交付，不提交。

## 备选方案与检查点
拒绝只移 HTML 后留下 2300 行后端；拒绝按行数切片 exec；拒绝把整个模块上下文注入每个 helper；拒绝重新设计 HTTP 路由和状态机。检查点：基线→函数/模板等价搬迁→测试所有者迁移→后验+独立评审。无新的锁/事务/缓存/线程模型，既有后台 Job 语义保持。

## 方案评审记录
独立只读 reviewer console_plan_review：发现 console/http.py 会遮蔽标准库 http 并阻止直接脚本启动，已采纳改名 http_server.py。其余方案未发现阻塞。跨 cwd 真实子进程启动测试为必须通过项。
