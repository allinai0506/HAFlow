# Controller 可视化与工作流解卡控制台 (Controller Visualization & Unblocking) 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Controller 的后台调度与卡点控制能力前台可视化，在出现工作流卡点（测试失败、Agent 空转、门禁阻断、终化挂起等）时，将决策建议转化为带具体 CLI 命令行透出与一键执行的结构化解卡方案，彻底解决“工作流卡住不知道怎么控制”的痛点。

**Architecture:**
1. **控制动作解析引擎 (`herdr/controller_actions.py`)**：纯函数/领域核心，根据工作流当前阻塞态（`stage_verdict=='blocked'`、`failed`、`rework`、`is_stalled`、`superseded` 关系）生成结构化的解卡方案（`ControllerAction`），包含方案分类、人类解释、对应的精准 CLI 命令行代码（`bin/herdr-task ...`）以及可执行的 API 参数。
2. **控制台 API 扩展 (`console/herdr_factory_console.py`)**：暴露 `/api/workflow/controller-actions`（查询当前活跃阻塞与推荐控制方案）与 `/api/controller/execute-action`（执行解卡操作并记录 Trajectory 审计事件），并修复后端 `_blocked_verdict_tasks` 与前端 `isDecisionTask` 漏过滤已取代（`superseded`）任务造成的幽灵决策 Bug。
3. **前台解卡卡片与控制台驾驶舱 (Console UI)**：在“人机协同态势”告警条中增加可交互的决策卡片（方案标题、依据、命令行代码块、复制按钮、一键执行按钮）；在顶部新增“🎮 Controller 调度控制台”抽屉，提供调度器等待状态观测与常用控制指令速查面板。

**Tech Stack:** Python 3.10+, 原生 JS/HTML/CSS (Console SPA), pytest, existing StateStore & bin/herdr-task CLI.

## Global Constraints

- 严禁引入不必要的第三方外部包（Ponytail 原则，使用标准库与既有依赖）。
- 严禁在生产或主干直接开发，所有修改在沙盒 `feat/controller-update` 分支中进行。
- 保证历史兼容：既有 `/api/workflow`、`/api/ops-center` 等接口不变，新增 API 采用非破坏性命名。
- 保证 JS 语法严谨：每次前端修改必须通过 `pytest tests/test_console_frontend_syntax.py`（经由 `node -c` 语法检查）。
- 不破坏现有安全边界：命令执行必须校验参数合法性，受控调用既有 `herdr-task` / `herdr_kernel` 控制原语。

---

### Task 1: 核心控制方案生成引擎 (`herdr/controller_actions.py`)

**Files:**
- Create: `herdr/controller_actions.py`
- Test: `tests/test_controller_actions.py`

**Interfaces:**
- Consumes: Task mappings, Workflow mappings from StateStore.
- Produces:
  - `ControllerAction` (dataclass)
  - `resolve_workflow_blockers(tasks: list[dict], workflow: dict) -> list[dict]`
  - `generate_controller_actions(task: dict, workflow: dict, project_root: str) -> list[ControllerAction]`
  - `build_cli_command(action_type: str, params: dict) -> str`

- [ ] **Step 1: 编写失败的单元测试**
在 `tests/test_controller_actions.py` 中编写针对不同阻断场景的生成测试：
1. 测试失败场景（测试未通过，生成【派发 Fix-Loop 修复任务】与【换 Agent 重新测试】方案，命令行包含 `--stage implementation` 与 `--stage test`）。
2. 空转/多次 rework 场景（生成【换 Agent 重派】方案，命令行包含 `--supersedes` 与备选 Agent）。
3. 门禁阻断场景（生成【门禁豁免/强制放行】方案，命令行包含 `herdr-task advance`）。
4. 过滤已取代任务：已标为 `superseded` 的任务绝对不得出现在活跃 blocker 列表中。

- [ ] **Step 2: 运行测试并验证失败**
执行 `pytest tests/test_controller_actions.py -v`，预期报 `ModuleNotFoundError`。

- [ ] **Step 3: 编写最小实现**
在 `herdr/controller_actions.py` 中实现：
1. 数据结构 `ControllerAction`（包含 `action_id`, `title`, `description`, `category`, `command_line`, `api_endpoint`, `api_payload`, `is_destructive`, `recommended`）。
2. `resolve_workflow_blockers`：过滤掉 `status == 'superseded'` 的任务，只保留真正处于 `stage_verdict == 'blocked'`、`failed`、`rework` 或阻塞状态的活跃任务。
3. `generate_controller_actions`：针对测试失败（提取缺陷要点生成 fix-loop prompt）、任务空转（生成带有 alternative agent 的重派命令）、门禁阻断（生成 advance/force-pass 命令）。
4. `build_cli_command`：格式化标准的 `bin/herdr-task` 命令行字符串。

- [ ] **Step 4: 运行测试并验证通过**
执行 `pytest tests/test_controller_actions.py -v`，预期 PASS。

- [ ] **Step 5: 提交代码**
```bash
git add herdr/controller_actions.py tests/test_controller_actions.py
git commit -m "feat(actions): add controller action resolution engine and CLI builder"
```

---

### Task 2: 控制台 API 扩展与幽灵决策过滤 (`console/herdr_factory_console.py`)

**Files:**
- Modify: `console/herdr_factory_console.py`
- Test: `tests/test_console_controller_actions_api.py`

**Interfaces:**
- Consumes: `herdr.controller_actions`
- Produces:
  - `GET /api/workflow/controller-actions`
  - `POST /api/controller/execute-action`
  - 修正后的 `_blocked_verdict_tasks(wid)`

- [ ] **Step 1: 编写 API 单元测试**
在 `tests/test_console_controller_actions_api.py` 中测试：
1. `GET /api/workflow/controller-actions` 返回结构化的 blockers 和 actions，验证返回了包含 `command_line` 的解卡方案。
2. 验证已 `superseded` 的任务不出现在返回的 blockers 中。
3. `POST /api/controller/execute-action` 接收合法 action 并正确转发给底层 `herdr_kernel` 或 `herdr_task` 处理。

- [ ] **Step 2: 运行测试并验证失败**
执行 `pytest tests/test_console_controller_actions_api.py -v`，预期因接口未注册报 404 或 ImportError。

- [ ] **Step 3: 在 `herdr_factory_console.py` 中实现后端端点**
1. 导入 `herdr.controller_actions` 中的函数。
2. 实现 `api_workflow_controller_actions(wid)`：获取工作流配置、活跃任务、生成 actions 并封装响应。
3. 实现 `api_controller_execute_action(payload)`：根据 action 类型分流执行（例如 `relaunch` 调 `herdr-task launch`，`force_pass` 调 `herdr_kernel.force_pass_gate`，`advance` 调 `manual_advance` 等）。
4. 在 `do_GET` 中注册 `/api/workflow/controller-actions`。
5. 在 `do_POST` 中注册 `/api/controller/execute-action`。

- [ ] **Step 4: 运行测试并验证通过**
执行 `pytest tests/test_console_controller_actions_api.py -v`，预期 PASS。

- [ ] **Step 5: 提交代码**
```bash
git add console/herdr_factory_console.py tests/test_console_controller_actions_api.py
git commit -m "feat(console): add controller action endpoints and fix superseded blocker filter"
```

---

### Task 3: 前台待决策卡片改造（透出具体命令 + 一键执行）

**Files:**
- Modify: `console/herdr_factory_console.py` (HTML/JS 模板部分)
- Test: `tests/test_console_frontend_syntax.py`

**Interfaces:**
- Consumes: `/api/workflow/controller-actions`
- Produces:
  - 更新后的 `isDecisionTask(t)`：过滤 `t.status === 'superseded'`
  - 增强型 `updateAttentionHub()`：异步加载或渲染 Controller 解卡方案卡片
  - 命令行代码块展示组件：包含命令预览框与 `copyToClipboard()` 按钮
  - 一键执行交互逻辑：点击触发 `executeControllerAction()`

- [ ] **Step 1: 编写前端契约与语法测试**
在 `tests/test_console_frontend_syntax.py` 中追加测试：
1. 验证 JS 中 `isDecisionTask` 严格排除了 `superseded` 状态。
2. 验证模板中包含控制器命令复制与执行函数（`executeControllerAction`, `copyCliCommand`）。
3. 验证 JS 语法通过 `node -c` 检查无语法错误。

- [ ] **Step 2: 运行测试并验证失败**
执行 `pytest tests/test_console_frontend_syntax.py -k "decision or action" -v`，预期失败。

- [ ] **Step 3: 改造前端 HTML/JS**
1. 修正 `isDecisionTask(t)`：
   ```javascript
   function isDecisionTask(t){
     if(t.status==='superseded'||t.status==='cleaned') return false;
     return t.stage_verdict==='blocked'||t.status==='blocked'||(t.node_type==='gate'&&['agent_done','completed'].includes(t.status)&&t.stage_verdict!=='pass');
   }
   ```
2. 在 `updateAttentionHub()` 中引入 Controller 动作卡片渲染：
   - 提取活跃阻塞项的推荐方案。
   - 渲染每个方案的【方案名称】、【说明】。
   - 渲染带有命令行的终端风格展示区：
     `<div class="cli-cmd-box"><code>${esc(action.command_line)}</code><button onclick="copyCliCommand(...)">📋 复制</button></div>`
   - 渲染【一键执行】操作按钮：
     `<button class="btn primary" onclick="executeControllerAction(...)">🚀 一键执行</button>`
3. 实现客户端交互函数：
   - `copyCliCommand(text)`：复制命令到剪贴板并弹出 Toast 提示。
   - `executeControllerAction(action)`：弹出确认提示后异步调用 `/api/controller/execute-action`，完成后刷新工作流。

- [ ] **Step 4: 运行语法与前端契约测试**
执行 `pytest tests/test_console_frontend_syntax.py -v`，确保所有 JS 代码语法 Clean。

- [ ] **Step 5: 提交代码**
```bash
git add console/herdr_factory_console.py tests/test_console_frontend_syntax.py
git commit -m "feat(console-ui): render actionable decision cards with CLI command preview and execute buttons"
```

---

### Task 4: 前台“Controller 调度与解卡驾驶舱”全景面板

**Files:**
- Modify: `console/herdr_factory_console.py` (HTML/JS 模板部分)
- Test: `tests/test_console_frontend_syntax.py`

**Interfaces:**
- Produces:
  - 顶部导航栏按钮：【🎮 Controller 控制台】
  - 模态弹窗 / 抽屉：`openControllerCockpitModal()`
  - 全景展示：
    1. 调度器当前工作状态（心跳、等待中的依赖条件）
    2. 当前阻塞诊断矩阵（Blocked Nodes, Stalled Stages）
    3. 交互式解卡工具箱（可手动选择 Agent、阶段、一键组装 `herdr-task launch` 或 `advance`）
    4. 常用控制命令速查表（Launch, Supersede, Advance, Clear-Escalation, Pause/Resume, Rollback）

- [ ] **Step 1: 编写前端驾驶舱测试用例**
在 `tests/test_console_frontend_syntax.py` 中测试控制器驾驶舱模态框与组件的存在性与语法。

- [ ] **Step 2: 运行测试并验证失败**
执行 `pytest tests/test_console_frontend_syntax.py -k "cockpit" -v`，预期失败。

- [ ] **Step 3: 实现 Controller 驾驶舱界面与逻辑**
1. 顶部操作栏增加 `openControllerCockpitModal()` 触发按钮。
2. 弹窗内渲染三大模块：
   - **态势诊断区**：显示 Controller 当前是否停滞、卡在哪个节点、上游依赖完成情况。
   - **交互解卡区**：支持选择 Agent（如 codex, claude, opencode）一键生成并执行重派或推进命令。
   - **常用命令速查区**：显示真实生产环境解卡常用的 CLI 命令模板及参数含义，方便用户掌握底层原理。

- [ ] **Step 4: 运行语法与前端契约测试**
执行 `pytest tests/test_console_frontend_syntax.py -v`，确保 PASS。

- [ ] **Step 5: 提交代码**
```bash
git add console/herdr_factory_console.py tests/test_console_frontend_syntax.py
git commit -m "feat(console-ui): add dedicated controller cockpit modal and quick command toolbox"
```

---

### Task 5: 真实调用链验证与全量回归质检

**Files:**
- Test: 全量测试套件

- [ ] **Step 1: 运行所有控制台专项测试**
```bash
pytest tests/test_controller_actions.py tests/test_console_controller_actions_api.py tests/test_console_frontend_syntax.py tests/test_console_kernel_api.py -v
```
预期：全部通过。

- [ ] **Step 2: 针对真实工作流数据 (`wf-haflow-0924-01`) 执行一次离线 action 解析验证**
编写小型离线断言脚本，加载截图中的真实任务数据，断言：
1. `plan-attack`（已 supersede）不再作为活跃 blocker。
2. `test-r10`（当前失败）能够成功生成 Fix-Loop 修复命令与换 Agent 测试命令。
3. 生成的 CLI 命令符合 `bin/herdr-task` 的合法入参格式。

- [ ] **Step 3: 运行全量回归与编译检查**
```bash
python3 -m compileall -q herdr services bin tests console
git diff --check
pytest -q
```
预期：语法零错误，Git Diff 干净，全量回归绿灯。

- [ ] **Step 4: 提交验收结果**
```bash
git add .
git commit -m "chore(release): verify controller visualization and unblocking actions"
```
