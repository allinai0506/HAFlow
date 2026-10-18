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

## R2 交付限制（2026-10-18）

- F5：项目目标/简介采集与持久化尚未实现；概览只读既有注册表中的 goal/description，无字段时明确显示“未提供”，不宣称采集功能已实现。
- F6：仅支持已存在目录；未实现服务端新建文件夹与相应权限校验，页面已明示此限制。
- “需要我处理”聚合当前项目各需求的既有待决账本；不扩展 Task 生命周期。项目最近活动来自真实需求列表，无需求时显示无活动。

## R2 定向返工验收

基线为 `85c3a98eb1973f0fd1656e6557aa78268a067da2`。本轮沿工位分支 `agent/codex/fix-haflow-simple-workbench-v1-r2` 修改；下列记录针对本轮，前文 V1 数字为历史证据。

| 项 | 变更与验证 |
| --- | --- |
| F1 / D-1 | 模板、分类、搜索按 `project_id` 保存于页面内存。项目切换清空控件并失效旧模板请求。Node 回归覆盖 A→B→A 和延迟响应。真实浏览器重复验证业务 A 法务模板→Git B 模式默认→A 恢复法务选择。 |
| F2 | 名称与 description 业务场景搜索叠加分类筛选，保留原生选择器与对话框滚动。名称、场景、分类无匹配各有独立断言。 |
| F3 | 执行页显示 `/api/workflow` 返回的实际模板标识。浏览器真实启动隔离工作流后读回 `legal-r2`。缺失字段显示“未提供”。 |
| F4 | 内容区列出项目名称、已有目标和最近需求活动。当前项目各需求的待决记录聚合为“需要我处理”；点击进入对应需求。Node 断言两条需求同时出现，并拒绝其他项目的迟到响应。浏览器验证两需求聚合与裁决后仅余另一项。 |
| F5 | 采用上述显式未实现限制；无新的目标输入与持久化能力，不描述为已实现。 |
| F6 | 页面与本文明示仅支持已存在目录，未实现新建文件夹及权限校验。 |
| A3 | `/api/project/create` 在调用 `create_project` 之前初始化选定 StateStore。真实 HTTP 故障测试断言初始化失败时临时项目注册表不存在。成功创建→启动→SQLite→公开读取链沿用真实集成测试。 |
| C1 / C2 | submitter 为空时回退到表单提交按钮；URL 参数使用 `[...q].length`。两项独立回归都在冻结候选脚本上确认 RED。 |
| D2 | simple 页面中删除模板 ID 默认值；模板模式由 `templates_summary.execution_mode` 透传，新建项目选项也来自真实元数据。自定义唯一模板回归确认不依赖内置 ID。 |

测试使用既有 `tests/test_simple_workbench.py` 和 `tests/test_console_project_creation.py`。首批 7 项在修改前均失败；后续把搜索和兼容性检查拆成独立断言，并用只读 `git show 85c3a98:console/static/simple.js` 再次确认 C1、C2、名称搜索、场景搜索和 D2 的 RED。最新专项命令：

```bash
/opt/homebrew/bin/python3.13 -m pytest -q tests/test_simple_workbench.py tests/test_console_project_creation.py tests/test_execution_context_contract.py
```

结果为 78 passed。`node --check console/static/simple.js`、三个改动 Python 文件 AST 与 `git diff --check` 通过。Ruff 基线对照：console 文件 87→86，项目测试 4→4，simple 测试 0→0；没有新增诊断，整库仍有历史 lint 问题。

浏览器采用 ego-browser space 15 和工位内临时 sandbox，端口 8987。项目装配的终端传输与收费模型边界被隔离，模板、Factory 启动、SQLite、共享决策账本和公开 HTTP 路由保持真实。390px 窄屏没有横向溢出，截图已目视检查。一次聚合验证因误判 API 列表顺序而点击错误项并超时；该轮不计通过，随后按实际目标完成验证。浏览器空间已结束，不保留用户现场。

前两轮内循环分别为 4745 passed / 1 failed、4748 passed / 1 failed，均在 `test_rollout_policy.py::TestGuard::test_concurrent_operators_never_lose_an_update` 报 `2 != 1`。该文件及相关 rollout/state_db 实现相对基线无变化，单项独立运行及随后 30 次原样重复均通过。本轮没有修改该范围外测试或相关核心代码；全量最终结果另追加。

本轮只自审，独立洁净室双门禁待协调器在新候选重跑。未验证当前合并结果 CI、生产 UAT 或真实付费 Agent 生命周期，未推送、未合并、未部署。没有配置机器交付契约，自检分数不证明这些事项通过。启动现场已有的 `docs/bug-reports/2026-10-18-haflow-simple-workbench-v1-r2.md` 保持原内容，不列入本轮提交。

### R2 最终全量与候选状态

第三轮 `PYTEST_ADDOPTS='-n auto' /Users/user/HAFlow/bin/herdr-loop eval`：**4753 passed，3 warnings，102.76s；100.0，converged=true，test_exit_code=0**。未过滤任何测试，也未修改评估器或 rollout 测试。前两轮并发测试失败保留为已观察到的不稳定验收风险，第三轮通过不抹去该记录。评估器按既有 baseline-aware 规则计新增 lint 为 0，实际整库 lint 退出码仍为 1。

本轮增加 14 项回归，业务提交限 7 个授权文件。自审未发现范围内未修缺陷；候选可移交洁净室复验，不能据此宣称已通过独立双门禁或可合并。F5/F6 的未实现项按本次任务允许的显式限制方案交付。
