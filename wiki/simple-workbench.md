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

FACT：极简与空间工作台共用 `console/static/console-tokens.css`，空间工作台左侧顶部、极简工作台右上角的双向切换保留项目和需求身份。桌面左侧列出项目，窄屏改用选择器，仅查询所选项目。

FACT：页面围绕“新建项目 → 新建需求 → 获得成果”展开。首页呈现需求列表，进度和人工决定在原生弹窗中查看；成果按需打开，默认显示最多三项可用文件或链接，其余记录折叠。`/api/workflows` 直接投影 SQLite 既有 status 字段，缺失为 unknown。

FACT：新建需求先选择模板，排除 eng/sys 分类，保留自定义业务分类，默认匹配项目执行模式。模板搜索和详细说明折叠；模板声明的必填资料路径始终可见。需求说明必填，标题可选；未填写时以说明前80字符作为标题。项目仍需填写真实工作目录，没有上传能力。

FACT：原生 dialog 固定头尾、正文滚动，保留错误输入和错误焦点。新建项目初始焦点为名称，新建需求为模板；Esc 关闭后回到入口。进度弹窗定时读取状态，正在填写的决定和展开的明细暂停刷新，避免覆盖输入或打断阅读。

Evidence:
- `console/static/simple.{html,css,js}`
- `console/herdr_factory_console.py#api_workflows`
- `tests/test_simple_workbench.py`
- `docs/walkthroughs/20261018-simple-ui-unification.md`
