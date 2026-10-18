# 极简工作台 v3 本地实施验收

基线：`9fb898940e78f3881d9c6735eec269c557abc5b3`。独立 CoW 沙盒 `~/.sandboxes/haflow-simple-ui-20261018`，分支 `fix/simple-ui-unification`。本记录替代早期单列布局验收；主仓库和生产服务未更新。

## 变更

按用户批准的 v3 HTML 原型实施“新建项目 → 新建需求 → 获得成果”。左侧项目、紧凑需求列表、按需进度/成果，统一 Console 变量、字重、间距和原生弹窗。模板选择位于新建需求第一项，必填资料路径保留，标题可选。右上角切换保留项目和需求身份。

复用实际创建、启动、决定、成果接口。唯一后端增量是列表投影已有 SQLite status，缺失为 unknown；未新增 API、数据库表、依赖、上传能力或调度逻辑。清理隐藏选择器旧事件及重复描述。

## 自动化验证

沙盒内 Python3.13 执行 simple_workbench、console_project_creation、execution_context_contract、console_standard_layout_tabs、console_shell、console_flow_workbench、console_decision_ui、console_decision_api、deliverables、console_page_latency、console_workflow_ordering、console_workflow_latency 十二个专项：**219 passed in 8.10s**。

`node --check console/static/simple.js`、`python3.13 -m compileall -q console`、`git diff --check` 均通过。新增测试经历 RED→GREEN，覆盖模板过滤与默认、自定义分类、真实所选模板/资料提交、标题回退、SQLite 列表状态、重复 DOM ID 和弹窗轮询/草稿保护。原有 Git/context 模式及归属回归保留。

## 真实浏览器链路

ego-browser TaskSpace25，隔离服务8881，临时状态 `/tmp/haflow-simple-v3-runtime-20261018`。真实 Console Handler → Factory/SQLite → 读取 API；只替换终端及模型外部边界，不启动付费 Agent。

- 普通非 Git 目录创建“客户调研”；选自定义业务模板 reader-note-v3，必填资料缺失时阻止提交，提供真实路径后成功启动；SQLite 确认模板及 context_bindings。
- 进度弹窗保持打开，SQLite 状态变更后由原生15秒轮询显示“已暂停”。未提交的决定文本由专项验证保护。
- 实际 decision raise → 浏览器提交 → 待办消失；临时任务的真实 Markdown 文件可预览和下载，文件名与正文一致。该文件为验收夹具，不代表 Agent 业务交付。
- 390px、320px 页面无横向溢出；320px需求弹窗正文可滚动、提交区可见。模板加载后焦点位于模板；Esc 关闭回到新建需求。
- 当前入口保留 project_id/workflow_id；旧双向导航逻辑及专项保留。截图 `/tmp/haflow-v3-implementation-final-home.png`、`/tmp/haflow-v3-mobile.png`、`/tmp/haflow-v3-mobile-form-loaded.png`。

## 独立审查及边界

Standards/Spec 两位独立审查者核查基线至当前完整差异。首轮发现重复ID、进度弹窗未轮询，均已修复并补回归；最终两位均无剩余阻塞项，各独立运行 simple 模块33项通过。独立审查没有重复生产或浏览器全链路；浏览器证据由主控提供。读屏实测尚未执行。

本轮仅本地候选，没有推送、PR、合并、部署或重启8765。全量 CI、真实模型执行和业务 UAT 未验证。未引入需新增永久教训的复杂架构决策。

## PR #256 复核修正

GitHub Codex 指出成果页打开后未自动刷新。回归先复现无读取，再补结果视图复用既有 loadResults，预览和展开全部记录时暂停；保留序列归属校验。本地受影响专项 **220 passed in 8.50s**，独立复核该增量无阻塞（专项1 passed）。该修正不改变 API 或持久化。
