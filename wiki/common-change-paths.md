# 高频开发与代码修改指南 (common-change-paths.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> **面向 AI Coding Agent 与开发者的常见改动实战路径**  
> 关联索引: [[index]] | [[task-lifecycle]] | [[dag-workflow-engine]] | [[architecture]]

---

## 1. 场景一：新增或接入一款新的 AI Coding Agent CLI

当需要接入一款全新的 Agent（如 `cursor`, `gemini`, `qwen`）时，通常需要依序检查并修改以下链路：

1. **路由与准入白名单**:
   - `herdr/agent_router.py`:
     - 在 `DEFAULT_ALLOWED` 列表中追加该 Agent 名称。
     - 在 `DEFAULT_STAGE_PREFERENCES` 与 `DEFAULT_TASK_TYPE_PREFERENCES` 中配置其推荐阶位。
2. **轻量探针适配**:
   - `herdr/agent_binary.py`（agent id -> CLI 映射与二进制解析的单一事实来源，console / preflight / deep_preflight 共用）:
     - 在 `AGENT_BINARIES` 中注册内部标识到 CLI 名称的映射；安装位置不在服务 PATH 时确认 `EXTRA_BIN_DIRS` 覆盖。
   - `herdr/preflight.py`:
     - 在 `KNOWN_AGENTS` 中追加该 Agent 名称。
     - 在 `AUTH_HINTS` 中追加其本地凭证文件路径（若有）。
     - 在 `VERSION_ARGS` 中声明版本测试参数（通常为 `["--version"]`）。
3. **深度沙盒探针适配**:
   - `herdr/deep_preflight.py`:
     - 更新 `AUTH_HINTS`（`AGENT_BINARIES` 已统一到 `herdr/agent_binary.py`，无需在此重复）。
     - 检查其认证失败和配额耗尽的 CLI 标准错误输出，是否需要补充 `TOKEN_PATTERNS` 或 `AUTH_PATTERNS`。
4. **Worker 启动适配**:
   - `services/herdr-worker.py`:
     - 在 Pane 启动参数配置中，指定如何通过非交互模式（`--auto` / `-p` 等）引导该 Agent。
5. **服务热重载与验证**:
   ```bash
   # 1. 验证探针正常
   ./bin/herdr-preflight
   # 2. 验证深度探针
   ./bin/herdr-deep-preflight --deep
   # 3. 热重启 Controller
   launchctl kickstart -k gui/$(id -u)/com.user.herdr-controller
   ```

Evidence:
- `herdr/agent_router.py:DEFAULT_ALLOWED`
- `herdr/agent_binary.py:AGENT_BINARIES`
- `herdr/preflight.py:KNOWN_AGENTS`
- `services/herdr-worker.py`

---

## 2. 场景二：修改或新增 Task 状态机流转

当需要向任务生命周期引入新状态（如 `human_review` 或 `blocked_timeout`）时：

1. **核心状态机定义**:
   - `bin/herdr-task`:
     - 在 `TRANSITIONS` 字典中添加新状态，明确定义从哪些前置状态允许迁移进入，以及允许迁出至哪些下游状态。
2. **守护进程适配**:
   - `services/herdr-controller.py`:
     - 在 `reconcile_task_state` 中添加对该状态的处理分支。
     - 检查该状态是否影响 DAG 节点的完成判定（`is_node_complete`）。
   - `services/herdr-sentinel.py`:
     - 若该状态仍属于正在运行中的阶段，必须将其加入 `ACTIVE` 集合，否则看门狗将停止崩溃巡检。
   - `services/herdr-notifier.py`:
     - 若该状态需要人工介入（如等待人工审批），必须加入 `ATTENTION` 集合以触发系统通知。
   - `herdr/agent_router.py`:
     - 若该状态依然占据 Agent 工位与计算资源，必须在 `_active_agent_loads` 的 `active` 集合中保留，以维护负载均衡计数。
3. **重启与验证**:
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.user.herdr-controller
   launchctl kickstart -k gui/$(id -u)/com.user.herdr-sentinel
   launchctl kickstart -k gui/$(id -u)/com.user.herdr-notifier
   ```

Evidence:
- `bin/herdr-task:TRANSITIONS`
- `services/herdr-controller.py#reconcile_task_state`
- `services/herdr-sentinel.py:ACTIVE`
- `services/herdr-notifier.py:ATTENTION`
- `herdr/agent_router.py#_active_agent_loads`

---

## 3. 场景三：新增或调整业务 DAG 工作流模板

当为新业务领域创建一套专属的工作流模板时：

1. **编写 YAML 模板文件**:
   - 放置于 `workflow_templates/<name>.yaml`。
   - 遵循契约：声明 `name`, `label`, `version`, `nodes`。
   - 每个 node 必须具备唯一的 `id`，正确的 `depends_on` 依赖数组，明确的 `agent_policy` 与 `rules`。
2. **运行单元测试验证 DAG 合法性**:
   - `pytest` 会自动通过 `test_list_templates_bundled` 扫描所有内置模板并执行 Kahn 拓扑算法。
   ```bash
   pytest tests/test_workflow_engine.py
   ```
3. **CLI 查看模板列表**:
   ```bash
   ./bin/herdr-factory templates
   ```
4. **应用至项目**:
   - 在新项目根目录下执行 `./bin/herdr-factory project --template <name>`，系统为模板中的每一个 Node 创建一个 Tab。Tab 自带的窗格留给该阶段第一个任务。

Evidence:
- `workflow_templates/`
- `herdr/workflow.py#validate_workflow_dag`
- `tests/test_workflow_engine.py`

---

## 4. 场景四：修改阶段页签的窗格分配

当调整终端窗格分配策略或修改 `ensure_node_runtime` 时：

1. **核心逻辑定位**:
   - `herdr/projects.py#ensure_node_runtime`: 节点页签探活。页签没了就重建。
   - `herdr/topology.py#_resolve_or_create_anchor`: 选择下一次 split 的父窗格，并关掉多余空壳。
   - `services/herdr-worker.py#allocate_task_pane`: 空闲父窗格直接给任务，已被占用才 split。
2. **红线警示**:
   - 已经有 Agent 的窗格不能再启动第二个任务。
   - 不能关掉一个页签里的最后一个窗格，否则页签会一起消失。
   - 不能因为一个父窗格丢失而重新 provision 整个 Workspace。
3. **验证**:
   ```bash
   pytest -q tests/test_stage_pane_parent.py tests/test_fix_bug1002_config.py
   ```

Evidence:
- `herdr/projects.py#ensure_node_runtime`
- `herdr/topology.py#ensure_stage_topology`
- `RULES.md:拓扑动态自愈红线`

---

## 5. 场景五：后台守护进程逻辑变更与死锁救援

当修改了 `services/herdr-controller.py` 中的调度算法后：

1. **代码编译检查**:
   ```bash
   python3 -m compileall services/herdr-controller.py herdr/
   ```
2. **重启常驻 LaunchAgent**:
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.user.herdr-controller
   ```
3. **监控日志流**:
   ```bash
   tail -f ~/.herdr-controller/logs/controller.out.log
   ```
4. **死锁救援 (应急重置)**:
   - 若阶段推进卡在 `queued` 或 `notified` 导致总指挥无法接收新任务，检查并清理 `~/.herdr-controller/stage-state.json`。
   - 若 Agent 预占锁发生泄漏，检查并重置 `~/.herdr-controller/agent-reservations.json`。

Evidence:
- `CLAUDE.md:常用开发与运维命令`
- `CLAUDE.md:坑点 1：LaunchAgent 进程更新陷阱`
- `docs/operations/troubleshooting-faq.md`
