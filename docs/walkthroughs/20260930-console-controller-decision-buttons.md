# 20260930-console-controller-decision-buttons

> PR #120 · 分支 `feat/console-controller-decision-buttons` · merge commit `1132f68`

## 任务目标与背景

用户在控制台（`http://127.0.0.1:8765`）遇到两个具体问题：

1. **找不到按钮**。总指挥提示要对 `impl-t1-contract-foundation` 与
   `impl-t6-mock-retire-r3` 执行一次 `integrate`，但前端没有任何对应入口。
   排查后确认两点：
   - 那条提示**已经过期**——两个任务早已 `integrated`，其 `integrated_commit`
     （`760d2b02` / `2e4d1d46`）实测均为 `agent/gemini-init` 的祖先，重跑
     `integrate` 只会返回 `already_integrated`；
   - 真正的问题是 `herdr/controller_actions.py` 只为 `blocked/failed/rework`
     生成动作，`bin/herdr-task` 的 `commit/integrate/cleanup/finalize/
     clear-escalation` **至今没有 `action_id`**，前端不可能渲染出这些按钮；
     且 `resolve_workflow_blockers` 跳过 `completed/committed/integrated`，
     于是"无报错但仍待集成"的任务在 UI 上等同于"没事可做"。
2. **看不到需要人判断的事项**。Barrier-0 的 DU-10（MATCH 是否入 V1）、超管
   fail-closed 语义、复合索引三项裁决只以自由文本存在于 `workflow_docs`
   笔记正文与总指挥终端里；`dashboard.attention` 只收
   `blocked/failed/verdict=blocked/finalize_escalated`，决策类提醒完全不在前端。

目标：让"需要 Controller 执行"变成可点按钮，让"需要人判断"变成显式提醒。

## 改动范围与对比

### 动作面 — `herdr/controller_actions.py`（纯函数核心）

| 新增 | 说明 |
|---|---|
| `GIT_PIPELINE_FORWARD` | 交付链路逐步骤表，`(status, step, argv, label, needs_git)` |
| `generate_progress_actions()` | 健康任务的唯一下一步；返回可执行 argv |
| `collect_workflow_actions()` | 聚合 blocker + pipeline + paused 恢复，`action_id` 全局唯一 |
| `_escalation_actions()` | 终化升级三条处置（解锁 / 保留交付关闭 / 丢弃） |
| `_live_pane_actions()` | 真实 re-drive / steer / halt |
| `group` / `commands` / `command_base` 字段 | 分区渲染 + 可审计 argv |

**两个关键设计决策**（均由独立评审的阻塞项驱动）：

- **`needs_git` 逐步骤门控，而非整表门控**。首版把整张表 gate 在
  `integration_mode == "git"` 上，但 `finalize` / `cleanup` 只要求任务已落定
  （`TEARDOWN_BLOCKING_STATUSES`），与 git 无关。整表门控会让占多数的非 git
  任务（实测 246/327）仍然拿不到任何按钮——原缺陷在多数任务类别上原样保留。
- **`generate_controller_actions` 只经由 `resolve_workflow_blockers` 调用**。
  该函数总会追加破坏性的 `force_pass_advance` 兜底，对每个任务调用会在
  `cleaned` / `superseded` 历史任务上提供"强制放行"按钮。

**真实 re-drive vs steer**：前者是 `herdr agent prompt <pane> ... --wait` 直推
工位并等回执；后者只写进 steer 队列，由 Worker 在下个轮询点读取。这正是总指挥
提示里"真实 re-drive（而非注入 steer）"所指的区分。

### 提醒面 — `herdr/human_decisions.py`（新增）+ `herdr/dashboard.py`

决策账本的折叠规则：

- 只有带 `decision_id` 的笔记才是**待办**；普通 `kind=decision` 笔记是已裁定记录
  （否则每条历史裁定都会永久显示为待办）；
- 同一 `decision_id` 最新一条赢，`decision_status=resolved` 即关闭，可重开；
- `stale`（fix-loop 作废）不算活跃待办；
- `ADVICE_KINDS` 直接别名 `workflow_docs.NOTE_KINDS`——首版写成逐字副本，
  违反单一事实源红线（新增 note kind 会静默从建议时间线消失）。

`herdr/dashboard.py` 新增 `decisions` 段并并入 `attention`；顺带修正
`counts["attention"]` 原先按**切片前**总数统计、与渲染列表不一致的问题。

### 执行面 — `console/herdr_factory_console.py`

- `execute-action` 新增 `task_git_step` / `redrive` / `clear_escalation` /
  `supersede` / `close_workflow`；argv 一律回到核心表格解析（`_find_action` +
  `_run_action_command`），**不接受请求体携带的 `commands`**；
- `close-workflow --force` 移除：它绕过全部人工确认契约且无任何 action 声明它；
- `halt` / `steer` 改为复用既有进程内 `api_task_halt` / `api_task_steer`；
- `dashboard_data` 的决策扫描复用交付物扫描已读入的账本，消除每工作流二次
  `load_notes()`。

### CLI — `bin/herdr-task note-add --field KEY=VALUE`

只做解析与透传，字段名 / 保留名 / 长度校验仍由 `append_note(fields=...)` 统一
裁决，CLI 不自建第二套校验层。效果是总指挥（终端）与控制台（HTTP）可写同一种
决策记录。

## 验证与测试数据

```
pytest -q                                              → 2522 passed, 60 subtests (exit 0)
python3.13 -m compileall -q herdr services bin tests console → exit 0
git diff --check                                       → exit 0
```

线上实测（`install-herdr-console.sh` + `launchctl kickstart -k`）：

```
GET /api/workflow/controller-actions?workflow_id=wf-project-0929-01
  → blockers 0 / actions 3（全部 group=pipeline, recommended=true）
    impl-t1-contract-foundation:finalize   bin/herdr-task finalize impl-t1-contract-foundation
    impl-t6-mock-retire-r3:finalize        bin/herdr-task finalize impl-t6-mock-retire-r3
    impl-t5-frontend-workbench:commit      bin/herdr-task commit impl-t5-frontend-workbench
GET /api/workflow/decisions?workflow_id=wf-project-0929-01
  → decisions 0 / advice 12（真实 Barrier-0 gate/delivery 笔记）
GET /api/dashboard?workflow_id=wf-project-0929-01
  → counts 含 decisions 键
```

决策回写往返（临时 `HERDR_WORKFLOW_DOCS_DIR`，已清理；真实账本 `decision_id`
计数 0，未被写入）：

```
note-add --field decision_id=DU-10 --field decision_status=open
          --field 'options=["入 V1","不入 V1"]' --field recommended="入 V1"
  → /api/workflow/decisions 读回 options=['入 V1','不入 V1']
  → POST /api/workflow/decision 裁决
  → 待办清空，裁决记录进入 advice 时间线
```

**反向验证**（证明测试会红）：把核心表 `committed` 行改成 `commit` → 3 条测试
失败；注入缺失作用域 → 运行时测试捕获 `THREW ReferenceError`。

## 独立评审发现的两类缺陷（本 PR 自身引入）

首轮实现带着两个**全量测试 2494 项全绿**的功能级缺陷进入评审：

1. `controllerActionCard()` 提升到模块作用域后仍引用外层 `catMeta` →
   `ReferenceError`，整个 Controller 弹窗打不开，功能与改动前无异。
2. `openDecisionPanel()` 用 `JSON.stringify` 生成双引号内联 `onclick`，被 HTML
   属性解析器截断 → 选项芯片是死链。

根因：既有前端测试只有 `node --check`（仅证明可解析）与字面量 grep（与功能可用性
无因果关系）。`node --check` 发现不了 `ReferenceError`——作用域是运行期解析的。

修法：新增 `tests/test_console_cockpit_runtime.py`，真实抽取 `<script>`、Node +
DOM stub 下**实际调用**渲染函数，并对渲染出的每个 inline handler 逐条 `node --check`。
教训已沉淀至 `docs/lessons/lessons-learned.md` §35。

## 未验证项

浏览器人工点击未执行（本 session 无已连接桌面浏览器），已由 Node 运行时渲染测试
与线上 HTTP 实测覆盖。`impl-t5-frontend-workbench` 的 `commit` 按钮尚未在生产
工作流上点击执行。
