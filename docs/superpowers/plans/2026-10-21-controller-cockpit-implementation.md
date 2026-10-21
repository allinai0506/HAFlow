# Workflow Controller 驾驶舱 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 Workflow Controller 控制台升级为当前 Workflow 的唯一完整控制中心，集中现有 Controller 功能，展示确定性判断与真实执行回执，并支持模板级全自动/半自动策略。

**Architecture:** 保留 `services/herdr-controller.py` 及现有 Controller 状态机为唯一控制权威，在 `herdr/` 增加纯投影与控制策略函数，由 `console/` 负责读取权威快照、执行既有 Action、生成回执。现有 `controller-cockpit.js` 原地升级；Canvas、Task Drawer、Pane 和恢复入口只保留同源快捷定位，不创建第二套动作判断。

**Tech Stack:** Python 3 标准库、SQLite StateStore、现有 Console HTTP server、原生 JavaScript/CSS、pytest/unittest、Node runtime smoke tests、真实浏览器 E2E。

## Global Constraints

- 默认控制模式必须是 `automatic`；Controller 自动执行满足确定性前提的非破坏性动作，只有模板明确声明的 Node/Action 才进入 `confirmation_required`。
- `automatic` 不是“按钮免确认”：自动动作不等待用户点击。证据不足、身份未知、恢复冲突和 `is_destructive=true` 的动作不得自动执行，分别保持 blocked 或进入具备领域判断能力的人工入口。
- 人工确认不能绕过 Workflow、Task、Run、Pane、version、candidate 或 recovery identity 校验。
- 前端不能从 Task status 自行推断动作可用性；动作与 automation 状态由后端投影提供。
- 读取失败必须显示 `unknown` 或 `unavailable`，不能显示“暂无卡点”。
- 执行结果必须区分 `applied`、`accepted_waiting`、`partial` 和 `rejected`。
- Canvas、Task Drawer、Pane、Dashboard 和恢复提醒不保留独立控制逻辑，只调用同一 Action 或定位 Controller 驾驶舱。
- 复用 `console/static/console-tokens.css`；不得新增独立颜色体系或大面积深色背景。
- 不新增第三方依赖，不修改 Controller 的既有关键顺序，不用 UI 重构改写状态机。
- 所有源码实施必须在新的 CoW Task 沙盒中完成；本设计分支只保存规格与计划。

---

## File Map

### New files

- `herdr/controller_cockpit.py`：纯函数生成 Workflow-scoped Cockpit Projection、控制策略和结构化执行结果。
- `console/controller_cockpit_api.py`：读取权威快照、运行状态、Action、Recovery 与事件，装配 Cockpit API；执行后重新读取回执。
- `services/controller/cockpit_automation.py`：在 Controller sweep 中选择并提交当前可自动执行的 Action，使用持久幂等身份避免重复副作用。
- `tests/test_controller_cockpit_projection.py`：纯投影、控制策略、unknown 和结果分类测试。
- `tests/test_project_template_registration.py`：真实模板创建、注册、切换与 SQLite 配置快照测试。
- `tests/test_console_controller_cockpit_api.py`：真实 SQLite/API 适配、身份校验、部分成功和读取失败测试。
- `tests/test_controller_cockpit_automation.py`：自动执行、模板确认、破坏性拒绝、幂等和 Controller 顺序测试。
- `tests/test_console_controller_cockpit_e2e.py`：本地 Console + 浏览器 + SQLite/Pane 适配器的自验证 E2E。

### Modified files

- `herdr/workflow.py`：规范化并校验 `control_policy` 与 Node `control_mode/confirm_before`。
- `herdr/controller_actions.py`：扩展 `ControllerAction` 的 target、identity、automation、risk 和 evidence 字段；动作生成逻辑仍归现有纯核心所有。
- `console/control_api.py`：复用统一 Action 查找/执行函数，不再让不同入口独立拼接动作；保留兼容函数。
- `console/http_server.py`：增加 `/api/workflow/controller-cockpit`，并让 execute-action 返回结构化 receipt。
- `console/herdr_factory_console.py`：保持动态加载兼容导出，暴露新增 Cockpit API owner。
- `services/controller/registry.py`：在恢复处理之后接入 Cockpit automation，同时保持既有 Controller 关键顺序。
- `console/static/console.html`：引入 Cockpit 专用样式模块并保留现有脚本顺序。
- `console/static/console/styles/operations.css`：保留通用操作样式，移出或删除被新 Cockpit 样式取代的重复规则。
- `console/static/console/styles/controller-cockpit.css`：驾驶舱浅色布局、状态、卡片、时间线和响应式样式。
- `console/static/console/scripts/controller-cockpit.js`：集中渲染、定位、确认和 receipt 展示。
- `console/static/console/scripts/workflow-reader.js`：一次加载统一 Cockpit Projection，处理 request sequence 与读取失败。
- `console/static/console/scripts/flow-canvas.js`：节点操作改为定位驾驶舱，不再内嵌执行面板。
- `console/static/console/scripts/task-board.js`：Task 操作改为同源快捷定位；保留无 Workflow 上下文的只读入口。
- `console/static/console/scripts/task-detail.js`：Task Drawer 控制入口定位到驾驶舱目标 Task/Run。
- `console/static/console/scripts/dashboard.js`：Workflow 待办按钮打开对应 Workflow 驾驶舱，不直接执行动作。
- `console/static/console/scripts/workspace.js`、`console/static/console/scripts/sidebar-tabs.js`：支持 `controller_target` 深链与唯一驾驶舱视图状态。
- `docs/product-specs/workflow-template-schema.md`：记录模板控制策略、默认值、覆盖顺序与失败语义。
- `docs/guides/template-authoring-guide.md`：给模板作者提供 automatic/supervised 示例。
- `tests/test_workflow_engine.py`：模板策略 normalize/validate/round-trip。
- `tests/test_controller_actions.py`、`tests/test_controller_progress_actions.py`：Action contract 和 automation 投影。
- `tests/test_console_controller_actions_api.py`：兼容 API、身份绑定和 receipt。
- `tests/test_console_cockpit_runtime.py`：真实脚本渲染、浅色令牌、定位和四类 receipt。
- `tests/test_console_task_detail.py`、`tests/test_workflow_graph_projection.py`：旧入口集中化与节点定位回归。

---

### Task 1: 模板级自动与半自动控制策略

**Files:**
- Modify: `herdr/workflow.py`
- Modify: `herdr/projects.py`
- Modify: `docs/product-specs/workflow-template-schema.md`
- Modify: `docs/guides/template-authoring-guide.md`
- Test: `tests/test_workflow_engine.py`
- Create: `tests/test_project_template_registration.py`

**Interfaces:**
- Produces: `normalize_control_policy(workflow: dict) -> dict`
- Produces: `resolve_control_policy(config: dict, node_id: str, action_type: str) -> dict`
- Output contract: `{"mode": "automatic" | "confirmation_required" | "blocked", "source": "default" | "workflow" | "node" | "action" | "safety"}`

- [ ] **Step 1: Add failing normalization tests**

Add tests proving that an absent policy defaults to automatic, Workflow policy is inherited, Node policy overrides Workflow policy, and `confirm_before` overrides the Node mode for one action:

```python
def test_control_policy_defaults_to_automatic():
    cfg = normalize_workflow({"name": "x", "nodes": [{"id": "build"}]})
    assert resolve_control_policy(cfg, "build", "advance") == {
        "mode": "automatic", "source": "default",
    }


def test_node_action_confirmation_overrides_automatic_default():
    cfg = normalize_workflow({
        "name": "x",
        "control_policy": {"mode": "automatic"},
        "nodes": [{
            "id": "acceptance",
            "control_mode": "supervised",
            "confirm_before": ["advance"],
        }],
    })
    assert resolve_control_policy(cfg, "acceptance", "advance") == {
        "mode": "confirmation_required", "source": "action",
    }
```

- [ ] **Step 2: Run the focused tests and capture RED**

Run:

```bash
pytest -q tests/test_workflow_engine.py -k control_policy
```

Expected: FAIL because `resolve_control_policy` and policy normalization do not exist.

- [ ] **Step 3: Implement strict normalization and validation**

Implement these exact accepted values:

```python
CONTROL_MODES = {"automatic", "supervised"}


def resolve_control_policy(config, node_id, action_type):
    workflow_policy = config.get("control_policy") or {}
    node = next((n for n in config.get("nodes", []) if n.get("id") == node_id), {})
    if action_type in (node.get("confirm_before") or []):
        return {"mode": "confirmation_required", "source": "action"}
    if node.get("control_mode") == "supervised":
        return {"mode": "confirmation_required", "source": "node"}
    if workflow_policy.get("mode") == "supervised":
        return {"mode": "confirmation_required", "source": "workflow"}
    return {"mode": "automatic", "source": "default"}
```

Reject unknown modes, non-list `confirm_before`, duplicate action names and non-string entries. Preserve normalized policy in the immutable Workflow configuration snapshot.

- [ ] **Step 4: Document the schema and precedence**

Add the approved example and state the exact order: action override → Node mode → Workflow mode → automatic default. Document that confirmation never bypasses Controller validation and that runtime override is out of scope.

- [ ] **Step 5: Preserve policy through real project provisioning**

Update the existing project creation, `_register_project_workflow` and template-switching paths so the normalized top-level `control_policy` is copied into the immutable Workflow definition saved to SQLite. Add a test that starts from an actual template, provisions/registers the project Workflow, reads it back with `workflow_config_for`, and proves both supervised and explicit automatic Workflow policy plus provenance survive.

- [ ] **Step 6: Run Task 1 verification**

Run:

```bash
pytest -q tests/test_workflow_engine.py tests/test_project_template_registration.py
python3 -m compileall -q herdr tests
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit Task 1**

```bash
git add herdr/workflow.py herdr/projects.py docs/product-specs/workflow-template-schema.md docs/guides/template-authoring-guide.md tests/test_workflow_engine.py tests/test_project_template_registration.py
git commit -m "feat(workflow): add controller automation policy"
```

---

### Task 2: 统一 Controller Action 与 Cockpit Projection

**Files:**
- Create: `herdr/controller_cockpit.py`
- Modify: `herdr/controller_actions.py`
- Test: `tests/test_controller_cockpit_projection.py`
- Test: `tests/test_controller_actions.py`
- Test: `tests/test_controller_progress_actions.py`

**Interfaces:**
- Consumes: `resolve_control_policy(config, node_id, action_type)` from Task 1
- Produces: `build_cockpit_projection(*, workflow, config, tasks, graph, actions, recovery, controller, events, errors=None) -> dict`
- Produces: extended `ControllerAction.to_dict()` with `target`, `expected_identity`, `automation`, `risk`, `evidence_refs`
- Projection schema version: `controller-cockpit/v1`

- [ ] **Step 1: Add failing Action contract tests**

Assert that every generated action has a precise target and carries identity fields already available on its Task:

```python
def test_live_pane_action_binds_current_task_and_pane_identity():
    task = {
        "task_id": "T1", "workflow_id": "wf-1", "node": "build",
        "status": "working", "version": 7, "pane_id": "w1:p2",
        "run_id": "run-9",
    }
    actions = generate_progress_actions(task, {"workflow_id": "wf-1"})
    redrive = next(a for a in actions if a.api_payload.get("type") == "redrive")
    assert redrive.target == {
        "workflow_id": "wf-1", "node_id": "build", "task_id": "T1",
        "run_id": "run-9", "pane_id": "w1:p2",
    }
    assert redrive.expected_identity == {"task_version": 7, "pane_id": "w1:p2"}
```

- [ ] **Step 2: Add failing projection tests**

Cover:

- current judgment reports active work and next ready nodes;
- unresolved recovery suppresses conflicting launch/rework actions;
- missing controller runtime becomes `status: unknown`, not healthy;
- stale/historical Task actions are excluded;
- automation is resolved from Task node and action type;
- events are bounded and ordered newest-first.

Use a projection assertion shaped like:

```python
assert projection["schema"] == "controller-cockpit/v1"
assert projection["judgment"] == {
    "now": "implementation_running",
    "blocked_reason": "dependencies_incomplete",
    "next": "start_test_review",
    "requires_user": False,
}
assert projection["read_state"] == "confirmed"
```

- [ ] **Step 3: Run focused tests and capture RED**

Run:

```bash
pytest -q tests/test_controller_cockpit_projection.py tests/test_controller_actions.py tests/test_controller_progress_actions.py
```

Expected: FAIL for missing module and Action fields.

- [ ] **Step 4: Extend `ControllerAction` without breaking legacy consumers**

Append fields with safe defaults so existing construction sites and monkeypatch tests remain valid:

```python
target: Dict[str, Any] = field(default_factory=dict)
expected_identity: Dict[str, Any] = field(default_factory=dict)
automation: Dict[str, Any] = field(default_factory=lambda: {
    "mode": "automatic", "source": "default",
})
risk: str = "normal"
evidence_refs: List[Dict[str, str]] = field(default_factory=list)
```

Populate these fields in the existing generation functions; do not create a second action generator in `console/`.

- [ ] **Step 5: Implement the pure projection**

Keep `herdr/controller_cockpit.py` free of file, SQLite, subprocess and network I/O. It must:

- compute `judgment`, `nodes`, `todos`, `actions`, `timeline` and `read_state`;
- suppress only actions proven obsolete or recovery-conflicting;
- retain `unknown` for absent runtime/evidence;
- cap tasks, actions and timeline with explicit `*_truncated` flags;
- preserve action IDs and raw identity fields without synthesizing success.

- [ ] **Step 6: Run Task 2 verification**

Run:

```bash
pytest -q tests/test_controller_cockpit_projection.py tests/test_controller_actions.py tests/test_controller_progress_actions.py tests/test_console_plain_chinese_ui.py
python3 -m compileall -q herdr tests
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit Task 2**

```bash
git add herdr/controller_cockpit.py herdr/controller_actions.py tests/test_controller_cockpit_projection.py tests/test_controller_actions.py tests/test_controller_progress_actions.py
git commit -m "feat(controller): project cockpit actions and judgment"
```

---

### Task 3: Workflow-scoped Cockpit API、Controller 自动执行与真实回执

**Files:**
- Create: `console/controller_cockpit_api.py`
- Create: `services/controller/cockpit_automation.py`
- Modify: `console/control_api.py`
- Modify: `console/http_server.py`
- Modify: `console/herdr_factory_console.py`
- Modify: `services/controller/registry.py`
- Test: `tests/test_console_controller_cockpit_api.py`
- Test: `tests/test_console_controller_actions_api.py`
- Test: `tests/test_controller_cockpit_automation.py`
- Test: `tests/test_console_module_boundaries.py`

**Interfaces:**
- Consumes: `build_cockpit_projection(...)` from Task 2
- Produces: `get_controller_cockpit(workflow_id: str) -> dict`
- Produces: `execute_controller_action(payload: dict) -> {"receipt": dict, "cockpit": dict}`
- Produces: `run_automatic_controller_actions(workflow_id: str, *, deps) -> list[dict]`
- HTTP: `GET /api/workflow/controller-cockpit?workflow_id=<id>`
- HTTP: existing `POST /api/controller/execute-action` remains compatible and gains `receipt`

- [ ] **Step 1: Add a real SQLite fixture and failing GET tests**

Seed a Workflow, two Tasks, a recovery operation and events through the real StateStore. Assert that the API returns one transactionally coherent Workflow-scoped payload and never mixes a foreign Task:

```python
payload = controller_cockpit_api.get_controller_cockpit("wf-1")
assert payload["workflow"]["workflow_id"] == "wf-1"
assert {t["task_id"] for n in payload["nodes"] for t in n["tasks"]} == {"T1", "T2"}
assert all(a["target"]["workflow_id"] == "wf-1" for a in payload["actions"])
```

- [ ] **Step 2: Add failing receipt tests for all four outcomes**

Drive existing control functions with injected runners/stores and assert:

```python
assert result["receipt"]["status"] in {
    "applied", "accepted_waiting", "partial", "rejected",
}
assert result["receipt"]["action_id"] == requested_action_id
assert result["receipt"]["workflow_id"] == "wf-1"
```

Required cases:

- `applied`: pause/resume changes authoritative Workflow status;
- `accepted_waiting`: dispatch responsibility exists but Task is not registered;
- `partial`: force-pass applied and advance failed;
- `rejected`: expected Task version or Pane identity changed, with no mutation.

- [ ] **Step 3: Add failing Controller automation tests**

Prove that the Controller, without a browser request:

- executes an eligible `automatic` non-destructive action;
- does not execute `confirmation_required` and exposes it as a pending human action;
- never auto-executes `is_destructive=true`, force-pass or evidence-bypass actions;
- persists an idempotency identity before the external side effect and does not send it twice after restart;
- re-reads action identity immediately before execution and rejects stale Task/Pane/candidate facts;
- runs recovery reconciliation before automation and preserves the existing continuation → fix-loop → stage → Task event order.

Use controlled dependencies and a real temporary SQLite store; do not start a real Agent.

- [ ] **Step 4: Run focused tests and capture RED**

Run:

```bash
pytest -q tests/test_console_controller_cockpit_api.py tests/test_console_controller_actions_api.py tests/test_controller_cockpit_automation.py
```

Expected: FAIL for missing API and receipt schema.

- [ ] **Step 5: Implement one read adapter**

`get_controller_cockpit()` must read the authoritative Workflow/config/Tasks snapshot first, then bounded controller runtime and event facts. Optional runtime failures become `errors` entries passed to the pure projection; they do not erase the authoritative snapshot.

Do not call `workflow_detail()` and `api_workflow_controller_actions()` independently if that can observe different Task versions. Reuse one snapshot for graph, blockers, actions and recovery correlation.

- [ ] **Step 6: Centralize Action lookup and execution**

Add a helper in `console/control_api.py`:

```python
def resolve_controller_action(workflow_id, action_id, *, snapshot=None):
    """Rebuild the current action set and return the exact current action."""


def api_controller_execute_action(payload):
    """Validate current identity, execute, and return a structured receipt."""
```

The server must ignore client-authored `api_endpoint`, `commands`, `recommended`, `automation` and `risk`. It accepts `workflow_id`, `action_id`, required user input and expected identity, then re-derives the Action server-side.

- [ ] **Step 7: Add post-action authoritative readback**

Classify the receipt from facts, not from HTTP status:

```python
receipt = {
    "status": "accepted_waiting",
    "action_id": action.action_id,
    "workflow_id": workflow_id,
    "target": action.target,
    "operation_id": operation_id,
    "task_id": task_id,
    "message": "派发责任已建立，等待 Task 登记",
}
```

For `partial`, include `applied_steps` and `failed_step`. For `rejected`, include `reason_code` and `refresh_required`; do not report a mutation receipt.

- [ ] **Step 8: Add the Controller-owned automatic execution path**

`services/controller/cockpit_automation.py` consumes the same current Action projection as the UI, filters `automation.mode == "automatic"`, rejects destructive/bypass actions, and claims a persistent idempotency record before invoking the shared execution boundary. It records the same four-state receipt consumed by the Cockpit.

The registry/sweep integration must be fail-safe: an automation projection or execution failure records attention/receipt evidence and leaves the Workflow state unchanged. It must not call Console HTTP or import browser/Console rendering code.

- [ ] **Step 9: Preserve compatibility exports and routes**

Keep `/api/workflow/controller-actions` operational as a compatibility view over the new Cockpit Projection. Update `console/herdr_factory_console.py` dynamic compatibility bindings and module-boundary tests so historical import/monkeypatch callers still resolve the correct owner.

- [ ] **Step 10: Run Task 3 verification**

Run:

```bash
pytest -q tests/test_console_controller_cockpit_api.py tests/test_console_controller_actions_api.py tests/test_controller_cockpit_automation.py tests/test_console_module_boundaries.py tests/test_dispatch_recovery_ui.py
python3 -m compileall -q console herdr tests
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 11: Commit Task 3**

```bash
git add console/controller_cockpit_api.py console/control_api.py console/http_server.py console/herdr_factory_console.py services/controller/cockpit_automation.py services/controller/registry.py tests/test_console_controller_cockpit_api.py tests/test_console_controller_actions_api.py tests/test_controller_cockpit_automation.py tests/test_console_module_boundaries.py
git commit -m "feat(controller): automate cockpit actions with receipts"
```

---

### Task 4: 用现有浅色系统重构 Controller 驾驶舱

**Files:**
- Create: `console/static/console/styles/controller-cockpit.css`
- Modify: `console/static/console.html`
- Modify: `console/static/console/styles/operations.css`
- Modify: `console/static/console/scripts/controller-cockpit.js`
- Modify: `console/static/console/scripts/workflow-reader.js`
- Test: `tests/test_console_cockpit_runtime.py`
- Test: `tests/test_console_frontend_syntax.py`
- Test: `tests/test_console_ui_modules.py`

**Interfaces:**
- Consumes: `GET /api/workflow/controller-cockpit`
- Consumes: `POST /api/controller/execute-action` receipt contract
- Produces: `openControllerCockpit({workflowId, nodeId, taskId, runId, paneId, operationId})`
- Produces: `renderControllerReceipt(receipt)`

- [ ] **Step 1: Replace test fixtures with the approved Cockpit schema and add RED assertions**

Extend the Node runtime tests to assert the page renders:

- Controller service state and last check;
- current judgment: now / blocked reason / next;
- nodes/tasks/runtime identities;
- Controller todos and recovery cards;
- timeline receipts;
- `applied`, `accepted_waiting`, `partial`, `rejected` result copy;
- `read_state=unknown` as an error state, never “暂无待办”.

Add a token assertion:

```python
assert "var(--bg-page)" in html
assert "var(--bg-surface)" in html
assert "var(--primary)" in html
assert "#101828" not in cockpit_css
```

- [ ] **Step 2: Run runtime tests and capture RED**

Run:

```bash
pytest -q tests/test_console_cockpit_runtime.py tests/test_console_ui_modules.py
```

Expected: FAIL because the current cockpit renders the old action-centric modal.

- [ ] **Step 3: Add the Cockpit view state and stale-response guard**

Extend `state` with:

```javascript
controllerCockpit: null,
controllerCockpitSeq: 0,
controllerTarget: null,
controllerReceipt: null,
```

`loadControllerCockpit(workflowId)` increments the sequence and applies a response only if both sequence and Workflow ID still match.

- [ ] **Step 4: Implement the approved layout in `controller-cockpit.js`**

Refactor the existing file into small rendering functions:

```javascript
function renderControllerHeader(data) {}
function renderControllerJudgment(data) {}
function renderControllerNodes(data, target) {}
function renderControllerTodos(data, target) {}
function renderControllerTimeline(data) {}
function renderControllerReceipt(receipt) {}
function openControllerCockpit(target = {}) {}
```

Keep recovery decision forms and existing high-risk confirmations, but place them inside the matching Todo/Action detail. Do not duplicate Action execution in Node/Task renderers.

- [ ] **Step 5: Implement light-token CSS**

Use only existing tokens for page, surfaces, borders, text, primary and status colors. The dedicated stylesheet owns `.controller-cockpit-*`; delete superseded cockpit-specific declarations from `operations.css` instead of keeping two style sources.

- [ ] **Step 6: Render receipts and unknown states**

After an action:

1. render the immediate receipt;
2. refresh the Cockpit Projection;
3. keep `accepted_waiting` visible until its operation resolves or is superseded;
4. show `partial` with applied and failed steps;
5. on `rejected`, preserve the stale card, mark it outdated, and offer refresh.

- [ ] **Step 7: Run Task 4 verification**

Run:

```bash
pytest -q tests/test_console_cockpit_runtime.py tests/test_console_frontend_syntax.py tests/test_console_ui_modules.py tests/test_console_module_boundaries.py tests/test_console_plain_chinese_ui.py
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit Task 4**

```bash
git add console/static/console.html console/static/console/styles/controller-cockpit.css console/static/console/styles/operations.css console/static/console/scripts/controller-cockpit.js console/static/console/scripts/workflow-reader.js tests/test_console_cockpit_runtime.py tests/test_console_frontend_syntax.py tests/test_console_ui_modules.py tests/test_console_module_boundaries.py
git commit -m "feat(console): unify workflow controller cockpit"
```

---

### Task 5: 将分散入口收敛为驾驶舱快捷定位

**Files:**
- Modify: `console/static/console/scripts/flow-canvas.js`
- Modify: `console/static/console/scripts/task-board.js`
- Modify: `console/static/console/scripts/task-detail.js`
- Modify: `console/static/console/scripts/dashboard.js`
- Modify: `console/static/console/scripts/workspace.js`
- Modify: `console/static/console/scripts/sidebar-tabs.js`
- Test: `tests/test_console_cockpit_runtime.py`
- Test: `tests/test_console_task_detail.py`
- Test: `tests/test_workflow_graph_projection.py`

**Interfaces:**
- Consumes: `openControllerCockpit(target)` from Task 4
- Target keys: `workflowId`, `nodeId`, `taskId`, `runId`, `paneId`, `operationId`
- Constraint: contextual surfaces may render status and one “在 Controller 中处理” entry; they do not render executable Controller action cards.

- [ ] **Step 1: Add failing centralization tests**

Assert that:

- Flow Inspector no longer calls `executeControllerAction` directly;
- Task row/dropdown and Task Drawer open `openControllerCockpit({taskId: ...})`;
- recovery attention opens `openControllerCockpit({operationId: ...})`;
- Dashboard switches to the decision's Workflow before opening its cockpit;
- Pane shortcut includes both Task and Pane identity;
- the same Action ID is not rendered as executable in more than the Cockpit.

- [ ] **Step 2: Run centralization tests and capture RED**

Run:

```bash
pytest -q tests/test_console_cockpit_runtime.py tests/test_console_task_detail.py tests/test_workflow_graph_projection.py
```

Expected: FAIL because Flow Canvas and Task board currently execute actions inline.

- [ ] **Step 3: Replace inline action execution with typed deep links**

Use calls shaped like:

```javascript
openControllerCockpit({
  workflowId: state.workflowId,
  nodeId: node.id,
  taskId: task.task_id,
  runId: task.run_id || '',
  paneId: task.pane_id || '',
});
```

The Cockpit resolves the target against its current projection. If the target is historical or absent, it opens read-only context and explains that no current action is available.

- [ ] **Step 4: Remove duplicate action renderers and handlers**

Delete Flow/Task/Dashboard-specific Controller action card markup and direct `dashExec()` usage for Controller Actions. Keep unrelated Task detail, deliverables and dashboard navigation behavior unchanged.

- [ ] **Step 5: Verify keyboard and layer behavior**

Ensure Escape closes the topmost Cockpit detail before Task Drawer, focus moves to the located card, and returning preserves the selected Workflow/Node/Task context.

- [ ] **Step 6: Run Task 5 verification**

Run:

```bash
pytest -q tests/test_console_cockpit_runtime.py tests/test_console_task_detail.py tests/test_workflow_graph_projection.py tests/test_console_standard_layout_tabs.py
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit Task 5**

```bash
git add console/static/console/scripts/flow-canvas.js console/static/console/scripts/task-board.js console/static/console/scripts/task-detail.js console/static/console/scripts/dashboard.js console/static/console/scripts/workspace.js console/static/console/scripts/sidebar-tabs.js tests/test_console_cockpit_runtime.py tests/test_console_task_detail.py tests/test_workflow_graph_projection.py
git commit -m "refactor(console): centralize controller entrypoints"
```

---

### Task 6: 真实调用链 E2E、清理、验证与文档收口

**Files:**
- Create: `tests/test_console_controller_cockpit_e2e.py`
- Modify: `docs/superpowers/specs/2026-10-21-controller-cockpit-design.md`
- Modify: `wiki/architecture.md`
- Modify: `wiki/log.md`
- Modify: `docs/lessons/lessons-learned.md` only if implementation reveals a reusable failure pattern

**Interfaces:**
- Verifies: browser → Console HTTP → Action API → Kernel/CLI/Store/Pane adapter → SQLite/readback → Cockpit receipt
- Does not call paid models or write production state.

- [ ] **Step 1: Add the E2E fixture**

Start Console on a temporary port with temporary `HERDR_STATE_DB`, project files and controlled `herdr`/`herdr-task` adapters. Seed one Workflow containing:

- one working Task with Run/Pane identity;
- one waiting dispatch operation;
- one blocked gate;
- automatic and supervised Nodes.

- [ ] **Step 2: Add browser journey tests**

Exercise these journeys against the real page:

1. open Canvas → blocked Node → Controller Cockpit target;
2. eligible automatic action executes through Controller without a browser click;
3. supervised action displays evidence and confirmation;
4. steer/redrive/halt use the current Task/Run/Pane identity;
5. stale version is rejected and UI refreshes;
6. accepted dispatch remains waiting until the test adapter records Task registration;
7. force-pass + advance failure renders `partial` and does not repeat force-pass;
8. recovery read failure displays “状态未确认”.

After each mutation, query SQLite or the controlled Pane adapter and assert the UI matches the backend fact.

- [ ] **Step 3: Run E2E RED before final wiring fixes**

Run:

```bash
pytest -q tests/test_console_controller_cockpit_e2e.py -x
```

Expected before final integration fixes: at least one journey fails on missing readback or targeting. Record the failing assertion; do not weaken it.

- [ ] **Step 4: Make the smallest integration corrections**

Change only files already named by Tasks 1–5. If a newly discovered root-cause dependency is required, amend the Entry Gate and this plan before editing it.

- [ ] **Step 5: Perform mandatory S4 deletion-first cleanup**

Use `ai-slop-cleaner` in Mode B on the Task 1–5 diff. Specifically check for:

- duplicate Action lookup or result classification;
- obsolete inline Controller handlers;
- duplicate cockpit CSS in `operations.css`;
- wrappers that only forward unchanged dictionaries;
- unused compatibility aliases.

Any cleanup edit invalidates prior intersecting test evidence.

- [ ] **Step 6: Run fresh post-cleanup verification**

Run:

```bash
pytest -q tests/test_workflow_engine.py tests/test_controller_actions.py tests/test_controller_progress_actions.py tests/test_controller_cockpit_projection.py
pytest -q tests/test_console_controller_cockpit_api.py tests/test_console_controller_actions_api.py tests/test_controller_cockpit_automation.py tests/test_dispatch_recovery_ui.py
pytest -q tests/test_console_cockpit_runtime.py tests/test_console_task_detail.py tests/test_workflow_graph_projection.py tests/test_console_frontend_syntax.py tests/test_console_ui_modules.py tests/test_console_module_boundaries.py
pytest -q tests/test_console_controller_cockpit_e2e.py
python3 -m compileall -q herdr console services tests
python3 -m py_compile services/herdr-controller.py console/herdr_factory_console.py
git diff --check
```

Expected: every command exits 0. Record command, HEAD, exit code and affected test count in `.omc/verify-<session>.md`.

- [ ] **Step 7: Run real browser acceptance with the page left on the Cockpit**

Start the local service using temporary state, use `$ego-browser` to perform the approved journeys, capture screenshots for wide and narrow layouts, and read back SQLite/Pane state. Leave the browser on the inspected Controller Cockpit, not on an unrelated page.

- [ ] **Step 8: Update architecture and product documentation**

Document that:

- Controller remains the backend control plane;
- Cockpit is a Workflow-scoped projection and action surface;
- other pages are contextual deep links;
- automation policy comes from immutable Workflow config;
- receipts distinguish applied/waiting/partial/rejected.

Append one concise `wiki/log.md` entry. Add a lessons entry only if implementation exposed a reusable engineering lesson not already recorded.

- [ ] **Step 9: Run S6 review gates**

Run the repository health comparison, `google-code-review`, and spec-bearing `code-review` against the approved design and this plan. Any finding returns through S4 → fresh S5 → S6. After three NEEDS_FIXES rounds, stop and escalate.

- [ ] **Step 10: Commit Task 6**

```bash
git add tests/test_console_controller_cockpit_e2e.py docs/superpowers/specs/2026-10-21-controller-cockpit-design.md wiki/architecture.md wiki/log.md
git commit -m "test(console): verify controller cockpit end to end"
```

Do not push, create a PR, merge, deploy or restart services without separate authorization and fresh S6 `MERGE_READY` evidence.

---

## Implementation Checkpoints

1. **After Task 2:** review the template policy and pure Cockpit Projection before any HTTP/UI work.
2. **After Task 3:** review Action identity, server-side re-derivation and four receipt outcomes before rendering them.
3. **After Task 5:** visually review the shallow Console layout and prove old entrypoints no longer execute independent actions.
4. **After Task 6:** require fresh self-verifying E2E, S6 review and explicit delivery authorization.

## Deferred Product Debt

- Runtime override of automatic/supervised mode for one active Workflow.
- Cross-Workflow global Controller operations center.
- User capability profiles and organization approval chains.
- Predictive scheduling and long-term Controller trend analysis.

These items must not be smuggled into Tasks 1–6.
