# 极简工作台 V1 验证记录

## 范围与调用链

本工位新增 `/simple`，保留 `/` 专业工作台；未部署、未重启生产、未推送或创建 PR。
任务描述中的未提交页面不在工位基线中，初始 `verify-baseline` 返回 BASELINE_MATCH，故本工位从现有 API 补齐实现。

- 项目：`GET /api/projects` → 原有 `projects.json`；`POST /api/project/create` → `projects.create_project` → 既有装配与项目注册，写路径初始化选定 SQLite StateStore。
- 模板：`/api/templates` 分类、`/api/template` 详情；新增 execution_mode/context 元数据供页面展示与填写。
- 执行：`POST /api/run` → 后台 job → `herdr-factory run` → 原有启动/预检/注册；页面解析确切 `WORKFLOW_ID=`，不猜测最新需求。
- 人工决定：原有 `/api/workflow/decisions` 和 `/api/workflow/decision`，仍为共享文档账本追加记录。
- 成果：原有 deliverables 投影及带 workflow/task/成果 ID 的预览/下载端点；不以业务来源目录的任意文件冒充成果。
- 导航：双方链接传递 project_id/workflow_id；项目选择和需求选择以 URL 为准，刷新从 API 重载。

## 已执行证据（阶段记录）

1. TDD 初始四项均失败：页面 404、普通目录 Git 拒绝、业务模板缺失、context 参数缺失。最小实现后相关 99 项通过。
2. HTTP 集成：在临时目录创建普通业务项目，异步启动经过真实 Factory 核心并注册 SQLite；读回真实状态及冻结节点；人工提问/裁决后待决列表清空；真实文件预览与下载字节相同，跨 Task 读取 404。
3. 浏览器：ego-browser 空间 4，隔离服务 127.0.0.1:8877，真实页面操作完成关联项目、刷新恢复、选择业务模板、启动并显示工作流、提交人工决定、查看成果和下载 report.md、专业工作台往返保持项目和工作流。
4. 自定义类别：临时用户模板“法务合规 / 自定义合同评估”由真实模板 API 返回，页面显示其“合同审阅”节点及实际说明。
5. 桌面 1920px 与 390px 截图已目视检查；390px documentElement.scrollWidth 不大于窗口宽度。
6. 旧 Console 及受影响专项：389 passed，76 subtests passed。后续改动仍须重跑受影响专项；最终记录在完成阶段追加。

浏览器服务使用 `tests/support/simple_workbench_sandbox.py`：只替代外部终端/Agent 健康/信任及 CLI 传输边界；真实的 Factory 核心、项目文件、SQLite、模板、人工决策和文件读取未替换。浏览器中的工作任务和成果为隔离验收数据，不是生产 Agent 实际交付。真实付费 Agent、生产工作流自动继续和生产部署未验收。

## 已修边界

- 新装环境项目创建后没有 SQLite 时，需求列表不能误报空或查询时写库；在创建项目写路径初始化 StateStore。
- 空 project_root 不得被解析为仓库当前目录并启动工作流。
- 非 Git 目录使用 `business-task-v1`（Context Runtime）；Git 模板保留 Git 校验。
- 模板/工作流切换采用序号丢弃旧响应，决策输入期间不自动重绘；错误可见且可刷新重试。

## 交付限制

完整执行 Agent 生命周期未在测试环境启动；本地测试不能替代生产业务验收。全量 CI 及受管内循环评估结果必须按最终实际结果记录，不能用专项通过替代。

## 追加验证与修复

- 网络离线后点击刷新，页面显示“项目加载失败”，恢复网络后刷新可恢复；键盘 Enter 打开新建项目，Escape 关闭对话框。
- 工作流状态在隔离 SQLite 变为 failed 后，页面显示“执行失败”和专业工作台处理提示。
- Git 仓库子目录上的业务项目选择 Git 模板后，Factory 归一到根项目；已修复页面仍展示旧需求的问题。回归先 RED（business-child != git-parent）后 GREEN，浏览器确认显示“归一化导航回归”及真实根项目。
- 启动后页面读回失败不覆盖为成功；job 过期不永久禁用重试；下载请求失败在页面反馈。
- 第三轮全量：4738 passed / 3 warnings，内循环 100.0。此后针对读回失败反馈和沙盒 SystemExit 传输处理补回归，最终第四轮结果见下文。
- 新增 Python 文件 Ruff 检查通过。原 console/projects 两文件 Ruff 原始计数分别 86/12，与 HEAD 基线相同，无新增诊断；整库 Ruff 原始输出非零（6440 项历史诊断），不能宣称整库 lint clean。内循环沿既有 baseline-aware 规则计为 0 新问题。

## 最终验证

- 最终源码：第四轮 `PYTEST_ADDOPTS='-n auto' herdr-loop eval`，4739 passed、3 warnings，157.42s，评分 100.0，converged=true。并行参数与仓库 CI 的 xdist 模式一致，未过滤测试。
- 最终专项：434 passed、76 subtests passed；包含启动冲突失败回执、目录归一化归属及启动后读回失败反馈。
- `node --check console/static/simple.js`、改动 Python AST、`compileall`、`git diff --check` 均通过。未修改 CLI 可执行脚本。
- `verify-baseline` 列出 16 个范围内业务文件；内部身份与 loop 文件未纳入提交。
- 最后一次浏览器冲突验证用了过窄的预期错误文本而超时；HTTP 实际回执为 failed，原因为活动工作流阻止模板切换。其后 ego-browser 空间已不存在，未创建新空间绕过；该次精确 UI 文本断言不计 PASS。此前页面核心路径、导航、文件与窄屏验证有效；启动失败 UI 的处理另有 Node 及真实 HTTP 回归。
- 自审完成，未发现已知阻塞功能缺陷；未执行独立评审、当前合并结果 CI、真实付费 Agent 生命周期或生产业务 UAT。
- 未配置机器交付契约；内循环评分不认证业务验收、合并或部署。后续由协调器绑定候选与业务验收回执。
