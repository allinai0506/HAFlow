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
