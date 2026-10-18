# Simple UI Unification Implementation Plan

> **For agentic workers:** Execute inline with executing-plans; no additional agents required.

**Goal:** 统一极简/空间工作台视觉、入口与极简表单弹窗，并减少默认信息。

**Architecture:** 现有 Console HTML 模板与 simple 静态页面共享设计变量；原生 details/dialog 实现渐进展示。继续调用现有项目/工作流/决策/成果 API。

**Tech Stack:** Python HTTP、原生 HTML/CSS/JavaScript、pytest、ego-browser。

## Global Constraints
- 独立 CoW 沙盒；不改生产状态、不调用付费模型、不重启服务。
- 先行为回归 RED，再最小实现；现有真实 HTTP/SQLite 集成回归保留。
- 不新增依赖、后端接口、业务状态或推断成果优先级。

### Task 1: 信息层级与导航
Files: console/static/simple.{html,css,js}, console/static/console-tokens.css, console/herdr_factory_console.py, tests/test_simple_workbench.py.
- [x] RED：执行真实 JS，混合可读文件与重复提交记录，验证三项摘要/全部记录可展开；旧页面浏览器验证入口位置与长页症状。
- [x] 最小实现：共享 Console 变量，顶端双向入口复用 simpleWorkbenchUrl/contextUrl；移除全项目卡及查询；需求/任务/成果历史折叠，保留警告与所有记录。
- [x] GREEN：pytest 专项，node --check，真实浏览器上下文切换、折叠和无横向溢出。

### Task 2: 原生弹窗优化与回归
Files: console/static/simple.{html,css,js}, tests/test_simple_workbench.py, wiki/simple-workbench.md.
- [x] RED：打开现有表单检查首焦点、取消入口、长模板滚动，记录基线。
- [x] 最小实现：固定头尾、正文独立滚动、显式取消、字段说明和错误焦点；复用 dialog 原生键盘行为和草稿存储。
- [x] GREEN：真实隔离服务创建项目/启动需求/决策/文件预览下载；桌面和窄屏弹窗、Tab/Esc/返回焦点、失败输入保留。
- [x] 自审完整影响链，更新 Wiki 和验证记录，git diff --check；不部署或推送。

## 批准原型 v3 后的实施（替代前述单列布局）
- [x] RED：导航读取与切换、非代码模板过滤/默认、所选模板与必填资料真实提交。
- [x] 精修原型的字重、间距、主次按钮和折叠提示；将获批三步骨架迁入simple静态页面，保留现有API及目录/资料输入。
- [x] GREEN：真实HTTP/SQLite集成、相关专项、桌面/窄屏/键盘及现有 Git/context 模式回归。
- [x] ai-slop-cleaner限定本次文件，删除无用状态/重复展示，重跑专项。
- [x] google-code-review及code-review双轴独立审查，修复真实缺陷；记录未执行的生产与CI边界。
