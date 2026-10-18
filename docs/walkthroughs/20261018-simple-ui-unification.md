# 极简工作台统一与精简验收

基线：`9fb898940e78f3881d9c6735eec269c557abc5b3`。工作区：独立 CoW 沙盒 `~/.sandboxes/haflow-simple-ui-20261018`，分支 `fix/simple-ui-unification`。主仓库 `main` 未改动。

## 范围与数据边界

- 两个工作台共用原有 Console 设计变量；极简页移除宣传区、重复项目卡及全项目活动查询。
- 空间工作台切换入口从侧栏标题移至右上角，双向复用既有上下文 URL 能力。
- 默认显示当前需求、阶段、当前待决事项和三项可用成果。完整需求、执行历史、全部待办和其余成果可展开；部分读取错误、数量和不可读原因保留。
- 原生弹窗增加分组表单、固定头尾、滚动正文、取消操作和错误焦点；Esc 在搜索框内也能关闭，关闭后回到入口。

真实链路：浏览器 → Console Handler → 既有 project/create、run 和 decision 入口 → Factory/SQLite/共享文档 → 公开读取 API；成果预览和下载读取临时目录中的真实文件。未改调度、数据库 schema 或后端业务 API。

验证服务使用 `tests/support/simple_workbench_sandbox.py` 和 `/tmp/haflow-simple-ui-state-20261018`。仅替换终端配置及模型边界；没有启动真实 Agent、调用付费模型或改动生产状态。页面测试数据不代表 NexusArchive 业务 UAT。

## 自动化证据

在上述沙盒根目录执行：

```bash
/opt/homebrew/bin/python3.13 -m pytest -q tests/test_simple_workbench.py tests/test_console_project_creation.py tests/test_execution_context_contract.py tests/test_console_standard_layout_tabs.py tests/test_console_shell.py tests/test_console_flow_workbench.py tests/test_console_decision_ui.py tests/test_console_decision_api.py tests/test_deliverables.py
node --check console/static/simple.js
/opt/homebrew/bin/python3.13 -m compileall -q console
git diff --check
```

结果：187 passed（6.78s）；JS/Python 语法检查及差异格式检查退出码 0。新增行为经历 RED→GREEN：三项成果摘要、仅下载文件与安全链接、模板搜索焦点、搜索框 Esc、切换需求时立即清除旧决定、共享样式发布后重新校验缓存。

原“全项目活动卡”测试按已批准的新交互改为“选择器呈现全部项目、仅查询所选项目”；原实现限制说明保留在技术记录，不再要求展示在产品页面。未降低归属、读取或真实持久化断言。

## 浏览器证据

使用 ego-browser（TaskSpace 22），验证：

- 无效目录创建失败：错误获焦，名称和路径保留；修正后项目创建、需求启动成功，状态可读。
- 真实待决事项提交后公开读取为空；真实文件预览及下载正文一致，下载 HTTP 200。
- 五项可读文件默认展示三项，其余记录可展开；十条任务记录默认折叠；部分不可读工作区警告保留。
- 空间工作台入口位于顶栏（top=7.5px）；往返后 `project_id=business-d3f2789e` 与 `workflow_id=wf-project-1018-01` 保持一致；两页 `--primary` 都为 `#5e6ad2`。
- 390×720 视口：无横向溢出；需求表单正文滚动、提交区可见；项目弹窗边界 x=16、bottom=601.25，提交区可见。
- 新建需求初始焦点位于标题；Esc 关闭回到新建需求按钮；无匹配搜索不抢焦点。Tab 遍历未进入背景控件，浏览器原生行为允许进入浏览器工具栏；没有新增自定义焦点陷阱。

截图保存在 `~/.codex/task-receipts/haflow-simple-ui-20261018/`。预览端口 `8877` 使用隔离数据；生产端口 `8765` 未更新。

## 自审与交付边界

已自审完整受影响调用链。将决定表单移出原隐藏容器后，切换需求必须立即移除旧决定；该回归已修复并覆盖。共享变量样式使用 no-cache，避免发布后两页缓存不同版本。未引入依赖、并行状态源或新后端功能。

本轮为自审，没有独立评审。没有推送、创建 PR、合并、部署或重启生产服务；全量 CI 和真实 Agent 业务交付未验证。没有新增复杂根因或可复用架构决策，无需新增重复教训条目。

交互依据：[W3C 模态对话框模式](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/)、[GOV.UK 错误汇总](https://design-system.service.gov.uk/components/error-summary/)。按用户确认的当前 2026 年 10 月实践处理；未声称通过完整无障碍认证。
