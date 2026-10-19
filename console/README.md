# HAFlow 控制台 (HAFlow Console)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **标语：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  

产品名与标语以 `console/common.py` 中的 `PRODUCT_NAME` / `PRODUCT_TAGLINE` 常量为唯一事实来源：页面标题、侧边栏品牌区均由该常量注入，禁止在 HTML/JS 中散落硬编码。前端界面文案统一使用纯中文，领域名词按用户确认的映射翻译：**Tab=工作流节点/阶段、Pane=智能体工位、Agent=执行者、Task=任务、Workspace=项目空间、Workflow=工作流**（顶部指标卡固定为「项目空间」、「活跃工作流」；紧凑场景允许短形"工位/节点"）；`auto`、Router、agent 名称（opencode/codex/claude 等）与后端状态枚举及 API 协议字段保持原样。

这里是 HAFlow 控制台的仓库内 canonical source。此前 Console 只有部署副本 `~/.herdr-console/herdr_factory_console.py`，导致前端改动无法随仓库审查、同步和回滚。

## 技术栈

- **服务端**：Python 3，使用标准库 `http.server.BaseHTTPRequestHandler` 与 `ThreadingHTTPServer`，不依赖 Flask/FastAPI 等 Web 框架。
- **前端**：`console/static/console.html` 保存主页面模板；`console/page.py` 注入品牌常量。Simple 工作台及样式脚本也放在 `console/static/`。
- **接口格式**：JSON over HTTP，统一返回 `{ok, data, error}` 结构；`/api/ops-center` 代理 `bin/herdr-task ops-center`。
- **数据来源**：`console/common.py` 提供路径和子进程基础设施；`inventory.py` / `runtime.py` 读取状态和运行现场；`read_api.py` 投影详情、轨迹、成果和看板；`workflow_api.py`、`task_api.py`、`workflow_policy.py`、`control_api.py` 按操作职责装配既有 `herdr` 核心。`http_server.py` 独占 HTTP 认证、路由及响应格式。
- **运行与部署**：macOS LaunchAgent `com.user.herdr-factory-console`，默认监听 `127.0.0.1:8765`；仓库源通过安装脚本同步为 `~/.herdr-console` 运行副本。
- **构建依赖**：无 npm、Node.js、SPA 框架或前端打包步骤；页面模板随 Git release archive 部署，`console/` Python 包由启动入口直接导入。

## 运行架构

```text
console/herdr_factory_console.py  (薄启动 / 兼容导出)
        ├── http_server.py ──→ read_api / workflow_api / task_api / control_api
        ├── page.py ─────────→ static/console.html
        └── common / inventory / runtime / workflow_policy
                 │ scripts/install-herdr-console.sh: git archive release snapshot
                 ▼
~/.herdr-controller/releases/<commit>/console/
        │ com.user.herdr-factory-console LaunchAgent
        ▼
http://127.0.0.1:8765/
```

仓库源文件负责版本管理；`~/.herdr-console` 是运行时部署副本，不应直接作为长期开发位置。

## 部署

在仓库根目录执行：

```bash
./scripts/install-herdr-console.sh
```

脚本将完整 Git commit 打包到 `~/.herdr-controller/releases/<commit>`，校验快照并更新 LaunchAgent。运行时导入整个 `console/` Python 包，页面模板及静态资源留在 release 快照中；入口文件或模块不能单独复制部署。可使用 `--no-restart` 只发布配置，不激活服务。

## 任务归档查询 API

```text
GET /api/archive?project_id=&workflow_id=&agent=&status=&q=&limit=&offset=
  -> {total, count, limit, offset, status, items: [{task_id, workflow_id, node_label,
      agent, status, stage_verdict, goal, superseded_by, created_at, updated_at, duration_seconds}]}
```

数据源优先读取 StateStore（唯一事实源），`tasks.json` 仅作降级兜底——投影文件损坏或被覆盖时归档列表依然完整。`status` 支持组别名 `archived`（cleaned/superseded/failed，默认）、`active`、`all`，或任意精确状态名；`workflow_id`/`q` 为不区分大小写的片段匹配；按 `updated_at` 倒序、`limit` 上限 200。页面入口：动作区“任务归档”，每条任务可下钻既有「任务白盒简报」。

## Dashboard V2 API

Console 提供：

```text
GET /api/ops-center
GET /api/ops-center?workflow_id=<id>&include_tasks=1
POST /api/run              -> 202 {job_id, status: "running"}
GET /api/run/status?id=<job_id>
```

接口代理仓库内的 `bin/herdr-task ops-center`。普通工厂页面通过“进入运维驾驶舱”入口展示老板视角、Workflow/Tab、Agent Fleet、异常中心和任务时长；进入后同一位置显示“← 返回工厂”，点击即可恢复进入前的 Space/Workflow。运维视图会隐藏依赖工厂上下文的操作按钮；点击 Workflow 卡片则同步切换到它所属的项目和 Space，再进入对应 Workflow/Pane 详情。

新需求启动采用异步 Job：Console 不会让浏览器请求同步等待 Deep Preflight 和总指挥派发；后台命令允许最多运行 600 秒。启动成功后关闭弹窗并提示 Workflow ID；失败时保留表单、恢复按钮，并展示后端返回的具体错误。

## 工作流模板库 API

Console 通过 `sys.path` 直接复用 `herdr.workflow` 的模板引擎（`list_templates` / `load_template` / `validate_workflow_dag`）：

```text
GET  /api/templates          -> {templates: [{id, label, version, node_count, path, is_builtin}]}
GET  /api/template?id=<name> -> {template, nodes(含 depends_on), yaml(原文), is_builtin}
POST /api/template           -> {name, yaml}；服务端 DAG 校验通过后写入 ~/.herdr-controller/templates/<name>.yaml
POST /api/run                -> body 支持 template 字段，透传 herdr-factory run --template
```

页面入口：动作区“模板库”（列表 / 节点依赖预览 / 查看 YAML / 新建与编辑自定义模板）；“新需求”弹窗提供“工作流模板”下拉框。内置模板（`workflow_templates/`）只读；自定义模板名限 `^[a-z0-9][a-z0-9_-]{0,63}$`（防路径穿越），保存时先做未知依赖 / 循环依赖校验，失败即拒绝写盘。模板变更只影响之后新启动的 Workflow，不改运行中实例。

## 变更规则

- 前端代码只修改 `console/`，通过安装脚本部署。
- 后端数据聚合只修改 `bin/herdr-task`。
- 修改后至少执行 Console 语法检查、`/api/ops-center` HTTP smoke test 和浏览器页面检查。
- 不把 `~/.herdr-console` 下的备份文件、日志或缓存同步进仓库。

## 极简工作台 V1

入口 `/simple` 提供新建项目、新建需求、获得成果三个操作；`/` 保留专业工作台。两侧链接传递 `project_id` / `workflow_id`，刷新从服务端重载项目和需求。

- 业务目录选择 `business-task-v1`，通过既有 Context Runtime 执行，无需初始化 Git；软件项目与 Git 模板仍要求 Git 仓库。
- 模板选择器读取 `/api/templates` 的全部分类（含自定义），详情读取 `/api/template` 的真实节点、执行模式及 context 契约；必填资料路径必须填写。
- 启动后轮询原有 job 接口，以返回的 `WORKFLOW_ID=` 打开对应需求。job 状态仅在 Console 进程存活期间可查；重启后请从已登记需求中核对，避免重复启动。
- 人工决定复用共享文档决策机制。文件预览与下载复用既有成果归属/路径检查；“工作区文件”与“已产生”保留原始状态，不宣称已验收。
- 静态资产为 `console/static/simple.{html,css,js}`；无前端框架或构建依赖。`/simple` 的品牌来自 Python 常量。
- 新增 `GET /api/projects` 返回原项目注册表；创建项目的写路径初始化选定 StateStore，读取项目列表不探测终端。

专项：`python3.13 -m pytest -q tests/test_simple_workbench.py tests/test_console_project_creation.py tests/test_console_run_job.py tests/test_console_templates.py`。
隔离页面验收服务：`python3.13 tests/support/simple_workbench_sandbox.py --root <本工位临时目录> --port 8877`。该服务只替代外部终端与模型健康边界，业务 API / SQLite / 决策 / 文件为真实实现，不可用作生产服务。
