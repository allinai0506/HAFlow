# 极简工作台

FACT：`/simple` 是原生 HTML/CSS/JS 页面，和 `/` 专业工作台共用项目、工作流、模板、人工决策与成果 API。URL 携带项目和工作流身份，业务状态不保存在浏览器内。项目读取来自既有项目注册表，工作流及任务读取来自 SQLite。参见 [[flow-workbench]]、[[task-lifecycle]]。

FACT：普通业务目录使用 `business-task-v1` 及既有 context 执行路径；Git 模板仍需要 Git。`create_project` 按模板执行模式选择目录校验。模板分类按 `/api/templates` 动态呈现，不维护固定前端分类清单。

FACT：启动使用 `/api/run` 后台 job；页面只接受输出中的明确 `WORKFLOW_ID=`。Console 进程重启后内存 job 可能失效，应从持久化需求核对。人工裁决追加共享文档账本；成果文件由原有归属与路径检查端点读取。

UNKNOWN：本地隔离页面验证不证明真实付费 Agent 已完成业务交付或线上部署已激活。沙盒替代的是终端/模型外部边界，测试数据与生产无关。

Evidence:
- `console/herdr_factory_console.py#Handler`
- `console/static/simple.js#pollJob`
- `herdr/projects.py#create_project`
- `workflow_templates/business-task-v1.yaml`
- `tests/test_simple_workbench.py#test_real_http_create_start_decide_and_download`

FACT：极简与空间工作台使用 `console/static/console-tokens.css` 同一套设计变量；空间工作台右上角切换入口复用 `simpleWorkbenchUrl`，极简页使用 `contextUrl` 保留项目/需求归属。项目仅用选择器呈现，不再逐个查询全项目最近活动。

FACT：极简页默认展示当前需求/阶段、当前需求的待决表单和最多三项可查看、下载或打开的成果。完整需求、执行历史、项目全部待办及其余成果由原生 `details` 按需展开；记录数量和部分读取错误仍可见，不推断文件属于最终交付。展开详情期间暂停页面自动刷新，避免刷新收起正在阅读的记录。

FACT：新建项目、需求及成果预览使用原生 `dialog`，头尾固定、正文滚动；创建/启动失败保留输入并显示可聚焦错误。模板搜索无匹配时不抢走输入焦点；搜索框显式处理 Esc，避免浏览器先清空搜索而不关闭弹窗。项目名称/需求标题获取初始焦点，其余焦点约束及关闭回到入口使用浏览器原生能力。

Evidence:
- `console/static/simple.js#loadResults`
- `console/static/simple.js#formError`
- `console/static/simple.html`
- `tests/test_simple_workbench.py#test_result_summary_bounds_files_and_keeps_all_history_and_errors`
- `tests/test_simple_workbench.py#test_template_search_no_match_keeps_keyboard_focus`
- `tests/test_simple_workbench.py#test_escape_in_template_search_closes_dialog_without_clearing_draft`
