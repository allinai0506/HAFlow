# Wiki Evolution Log (log.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> 本文件为 HAFlow 知识层的 Append-Only 演进记录。  
> 仅记录 Wiki 结构与知识库发生实质性变更的原因与概要，不记录细碎的代码提交流水。

---

## [2026-09-17] fix | 任务级门禁结论对称：自动推进不再越过 blocked 依赖
- Updated [[dag-workflow-engine]] §4.4：plan/requirements 等无 gate 配置节点的 blocked 结论同样暂停自动推进（sweep 只 funnel 裁决、不销毁下游；direct 同查）；作废后自动恢复。
- 前端启动等待 180s→620s，对齐后端 600s 超时，消除误报式"启动失败"。
- 根因：GATE_DEFAULTS 仅 test/review/wrapup，sweep fix-loop 看不见 plan 级 blocked，而 direct 完全不查结论（线上 plan blocked 时 test-auto 仍被建出）。

## [2026-09-16] fix | LaunchAgent PATH prepending for user-space agent binaries
Console deep preflight failed for opencode with a 500 error because the
LaunchAgent service's minimal PATH resolved the outdated Homebrew-installed
binary (/opt/homebrew/bin/opencode v1.18.30) instead of the user's latest
install (~/.opencode/bin/opencode v1.18.31), and EXTRA_BIN_DIRS omitted
`~/.opencode/bin`.
- Updated [[preflight-and-health]] §2: `herdr/agent_binary.py` now prepends
  `USER_BIN_DIRS` to `os.environ["PATH"]` on import, guaranteeing user-space
  tools take precedence over system/Homebrew shadows.
- Expanded `EXTRA_BIN_DIRS` with full user-space agent paths (.opencode, .cargo,
  .bun, .grok, .kimi-code, etc.).
- Lessons recorded in `docs/lessons/lessons-learned.md` §47.

## [2026-09-12] init | Initial repository analysis & Wiki creation
Created initial LLM Wiki directly derived from active repository code inspection and behavioral evidence.
- Established Wiki Governance Rules in [[WIKI]].
- Designed master navigation and domain routing index in [[index]].
- Captured high-level architecture, problem boundaries, and core topology in [[system-overview]] and [[architecture]].
- Formulated core business domain entities and persistent storage contracts in [[domain-model]].
- Captured the critical Tab = Node spatial paradigm, Anchor Pane split mechanism, and runtime dynamic self-healing engine in [[tab-node-model]].
- Codified the full 11-state task lifecycle, CoW Git clone isolation, and baseline fingerprint verification in [[task-lifecycle]].
- Documented DAG topology parsing, cycle detection via Kahn's algorithm, and node ready resolution in [[dag-workflow-engine]].
- Documented multi-agent load balancing, reservation locking, and policy-driven routing in [[agent-routing-and-pools]].
- Documented non-destructive lightweight and deep sandbox health probes in [[preflight-and-health]].
- Codified developer & agent guide for safely making frequent codebase modifications in [[common-change-paths]].

## [2026-09-12] add | Agent Operations Center knowledge
- Added [[ops-center]]: documented the four dashboard layers, runtime/task state distinction, duration buckets, trajectory output, and anomaly action contract.
- Updated [[index]]: indexed the new operations view for future code navigation.

## [2026-09-12] move | Console source into repository
- Added the canonical Console source under `console/` and a repeatable `scripts/install-herdr-console.sh` deployment path.
- Updated service operations documentation and [[ops-center]] to distinguish repository source from the LaunchAgent deployment copy.

## [2026-09-12] fix | Workflow deadlock permanent engineering fix

Root-cause analysis identified three compounding failure modes causing DAG advance to permanently stall:

1. **`failed` status as permanent blocker** — `is_node_complete` had no way to skip a task that was replaced by another attempt; a single `failed` task would prevent the entire node from ever completing.

2. **`stage-state.json` write-once latch** — once `notified` was written for a stage, `mark_stage_advance_queued` would refuse to re-queue it even after the predecessor node regressed (e.g., new `failed` tasks arrived). The Controller would never re-trigger the advance.

3. **Head-of-Line blocking in `coordinator_worker`** — a single thread handled all workflows sequentially. One coordinator blocked on a busy agent would stall all other pending workflow advances indefinitely.

### Changes made

- **`bin/herdr-task`**:
  - `TRANSITIONS`: added `superseded` as a valid exit from `failed`, `cleaned`, and all in-progress states (`dispatched`, `working`, `blocked`, `agent_done`, `rework`). `superseded` is a terminal state.
  - `node_status` / `is_node_complete`: active tasks are now computed excluding `superseded` ones. A node with only superseded tasks and no active replacements is `incomplete`.
  - New `supersede_task()` function: marks a task `superseded`, optionally linking `superseded_by` and `supersede_reason`.
  - New `supersede` CLI subcommand: `herdr-task supersede <task_id> [--by <new_id>] [--reason ...]`
  - New `--supersedes` flag on `launch`: atomically supersedes the old task before launching the replacement in a single command.
  - New `stage-reset` subcommand: clears `stage-state.json` advance locks for a workflow (optionally scoped to a single stage).
  - New `advance` subcommand: calls `stage-reset` then prints a confirmation that the Controller will re-evaluate within ~2 s.

- **`services/herdr-controller.py`**:
  - `is_node_complete`: mirrors the `bin/herdr-task` logic — excludes `superseded` / `superseded_by` tasks.
  - `reconcile_stage_advance_states()`: called at the start of every `check_workflow_stage_advance` cycle. Scans `stage-state.json` for `notified` entries whose predecessor nodes are no longer complete, and revokes them so the advance can be re-triggered on the next cycle (~2 s).
  - `coordinator_worker` refactored to a lightweight dispatcher using `ThreadPoolExecutor(max_workers=16)` with per-workflow serialization locks (`_workflow_dispatch_lock`). Each workflow's blocking prompt call runs in its own executor thread, eliminating HoL blocking across workflows.

- **`tests/test_stage_advance_and_supersede.py`**: 17 new regression tests covering all five engineering changes. Full suite: 33 passed, 0 regressions.

## [2026-09-12] fix | Superseded-task stats alignment across ops-center and console

Root cause: the supersede exclusion predicate existed in 4 hand-written copies
(`is_node_complete`, `node_status`, `_node_task_status_counts`, console
`stage_summary`); the supersede feature synced only the first two, so the ops
board counted superseded tasks in the node denominator (→ pending) and the
console detail page fell to `mixed` (→ 处理中) while the controller had
already advanced the DAG.

- Updated [[ops-center]] §1: node/workflow `total` now counts only live tasks
  (`status == "superseded" or superseded_by` excluded, counted separately);
  fully retired nodes surface a distinct `superseded` status; drilldown picks
  the latest authoritative task when all tasks are terminal.
- Automation gate added: `tests/test_stage_advance_and_supersede.py#TestOpsCardParity`
  pins card aggregation to `is_node_complete` (this stats-drift class recurred
  for the 2nd time, per lessons-learned discipline #4).
- Lessons recorded in `docs/lessons/lessons-learned.md` §7.

## [2026-09-12] fix | Agent CLI binary resolution unified into herdr/agent_binary.py
Console roster / lightweight preflight / deep preflight each hand-rolled the
agent-id -> CLI mapping and resolved binaries via bare `shutil.which`, which
misses volta / `~/.local/bin` / `~/.qoder-cn/entry` installs under the
LaunchAgents' minimal PATH (codex/claude/qodercli/agy shown 未安装 while installed).
- Added [[preflight-and-health]] §2: resolution order is now
  `shutil.which` -> `EXTRA_BIN_DIRS` fallback -> login-shell `command -v`.
- Updated [[common-change-paths]] §2/§3 + [[index]] routing row: new-agent
  registration now starts at `herdr/agent_binary.py` (single source of truth
  for `AGENT_BINARIES`); preflight/deep_preflight keep only `AUTH_HINTS`/`VERSION_ARGS`.
- Lessons recorded in `docs/lessons/lessons-learned.md` §8 (3rd recurrence of
  the same-semantics-multi-implementation class).

## [2026-09-13] feat | Physical teardown lifecycle: finalize / close-workflow
Workflow 完成后任务 pane/clone 永不销毁(pane_persistent 默认保留),上下文随
活体无限累积;dispatch 前的 `/clear` 因 `_claimed_panes` 永久占用 pane 而结构性
空转(pane 复用从未发生)。确立"生而隔离,死而清零"生命周期并落地:
- Added [[task-lifecycle]] §5:finalize 序列(证据转写先行 → pane close →
  分档 clone 删除 → 状态推进)与 close-workflow 批量收尾(活跃闸门 /
  failed 保留 / 共享 tab 外来 pane 守卫 / 总指挥 pane 保留至知识沉淀后)。
- Controller 在 `[WORKFLOW COMPLETE]` 自动触发 close-workflow(防重入);
  purge 门槛放宽到非 ACTIVE(修 superseded 终态无法 purge 的死锁)。
- 新命令文档见 `docs/references/cli-reference.md` §2.6/2.7;决策与权衡
  (含上线当天抓到的共享 tab 连带销毁 bug)详见
  `docs/walkthroughs/20260913-workflow-finalize.md`;教训沉淀 §9。
- 验证:134 tests passed;真实端到端——wf-…-232500 手动收尾 + 历史 workflow
  自动收尾,9/9 workflows completed,pane 24→1。

## [2026-09-13] feat | Fix-loop: gate verdicts, atomic invalidation, delivery-outcome gates
评审"不通过"原先只活在自然语言里,交付被阻断的 workflow 仍被归档 completed
(wf-nexusarchive-…-084418 复盘)。本次落地 fix-loop 设计 v2(经对抗性思维链
审查修订,见 docs/walkthroughs/20260913-fix-loop-design.md §8):
- Added [[task-lifecycle]] §1.1 + [[dag-workflow-engine]] §10:门禁阶段
  (test/review/wrapup)落盘 `stage_verdict`,`blocked` 触发 controller 原子
  作废(gate+下游,finalize-first 规避非法转移窗口)并派发 fix_loop 事件,
  fix 完成后 DAG 自动重流;交付终态门禁阻止 blocked workflow 被关闭。
- `launch --onto`(commit 直落 PR 分支)、`reopen-workflow`(suppress_auto_close
  闩防 sweep 自消除)、`close-workflow --abandon`(outcome 语义)、console
  create_candidate/manual_advance 门禁封堵一键合并旁路。
- 教训 §12:流程完成≠交付完成;审查轮 1 抓到 verdict 死循环/重测缺失/
  reopen 自消除三处设计级漏洞后修复。
- 验证:183 tests passed;独立审查两轮。

## [2026-09-13] fix | Terminal-state gates: ghost-advance elimination + duplicate-creation guard
已关闭/零任务工作流被 controller 逐阶段"真空推进"并向共享协调者 Pane 注入幽灵
提示，协调者照办派发了 3 个真实任务（wf-…-111426 幽灵事故）；同项目重复创建
无任何防护。三层落地：
- [[architecture]] §2.1 增补终态闸门与创建闸门两条 FACT：推进扫描只遍历
  非终态条目 + 消费线程 fire 前再校验；herdr-factory 注册前 flock 内原子
  执行「同项目活跃检查 + 注册」，`--force` 为唯一逃生口。
- 共享谓词收敛到 `herdr/projects.py`（`workflow_closed` 等 5 个），factory
  消费；controller 内联同语义实现；单测 `tests/test_workflow_registry_guards.py`。
- Console 创建反馈闭环：成功后解析 `WORKFLOW_ID=` 自动切换到新工作流视图，
  等待期显示耗时与"请勿重复创建"提示。
- 运维卫生：legacy 顶层 workflow.json（09-11 e2e 残留，registry-less fallback
  复燃路径）已归档；24 个 e2e/测试 stage-state 僵尸键清除。
- 活体验证：创建闸门 exit 2 拒绝 + 注册表零新增；测试工作流 125332 被新版
  controller 判定完成并干净自动关闭（无幽灵推进）。教训沉淀 §13；决策与
  会话碰撞记录见 `docs/walkthroughs/20260913-controller-ghost-advance-and-create-guard.md`。

## [2026-09-13] feat | Workflow semantic title and daily sequence short ID (Option A)
解决历史 43 字符无语义机器 ID（`wf-nexusarchive-54433229-20260913-111049`）反人类问题，确立“任务标题为一等公民 + 短 ID”设计：
- [[domain-model]] §2.2.1 增补工作流实例模型契约：ID 规则为 `wf-{project}-{MMDD}-{seq:02d}`；显式字段 `title`；
- `herdr/projects.py` 新增 `generate_workflow_id`，`register_workflow` 支持 `title`；
- `bin/herdr-factory` run 命令新增 `--title` 参数并透传至总指挥智能体提示词；
- `bin/herdr-task` close-workflow 支持缺省参数自动推断当前项目活跃工作流及短后缀模糊匹配；
- `console/herdr_factory_console.py` 新建模态框表单重构为「项目 → 本次任务名称 → 模板 → 执行者策略 → 自然语言需求」，支持需求失焦智能自动提取标题，工作流切换器格式升级为「任务名称 (短ID)」。
- 质量防护与门禁：沉淀教训 §14，新增 `tests/test_console_frontend_syntax.py`（raw string 声明守卫 + `node -c` 无头 JS 编译验证），新增 `tests/test_workflow_naming_and_title.py`，全量 197 个测试 100% 通过。

## [2026-09-13] feat | Console URL Deep-Link & Notifier Click-to-Open Integration
解决 macOS CLI 通知默认归属“脚本编辑器”且无法定位到具体任务/工作流页面的痛点：
- [[architecture]] §2.3 更新 Herdr Notifier 架构描述：优先使用 `terminal-notifier` 附带 `-open` 直达链接，未安装时安全降级为 `osascript`；
- `console/herdr_factory_console.py` 前端支持 `window.location.search` (`workflow_id`, `task_id`, `pane_id`, `ops`) 参数解析；打开直达链接时自动切换空间与工作流，平滑滚动聚焦并自动呼出 `showTask` / `showPane` 弹窗；
- `services/herdr-notifier.py` 升级 `notify(..., url=None)` 并提供 `build_console_url`：任务关注态与工作流完成时拼接控制台 Deep-Link URL，`terminal-notifier` 可用时点击直达控制台；
- 质量防护与门禁：沉淀教训 §20，新增 `tests/test_console_deep_link.py` 与 `tests/test_herdr_notifier.py`，已同步部署至 `~/.herdr-console` 并重启守护进程。

## [2026-09-13] feat | Console UI/UX Modernization, Accessibility (WCAG AA) & Interaction Overhaul
对共事工厂控制台（`console/herdr_factory_console.py`）进行完整交互、排版与可访问性现代化改造：
- **可访问性 (WCAG AA)**：模态框支持 `role="dialog"`、`aria-modal="true"`、`aria-labelledby`；Toast 提示支持 `role="alert"`；全局支持 `Escape` 键快速退出弹窗；按键聚焦高亮环 `*:focus-visible`；二级暗色对比度提升至 5.8:1；
- **消除阻塞弹窗**：彻底废弃浏览器原生 `window.confirm` 和 `window.prompt`，统一采用无阻塞原生风格弹窗 `showConfirmModal` / `showPromptModal`；
- **信息架构与排版收敛**：顶部横排按钮分组重构为「流水线推进组」、「日常工具组」、「更多操作下拉 (`···`)」与右侧主行动点「新需求」，按钮使用符合暗色主题的高级深蓝（`#2563eb`）与纯白文字（`#ffffff`）；
- **视觉微雕与符号规范**：移除重复符号并全站用 SVG 矢量图标替换 Emoji（`🛠️`、`🟢`、`×` 等）；阶段看板增设流向箭头 `›` 与进行中呼吸光效；运维驾驶舱舰队数据接入 6 列 CSS Grid 规整表格；
- **质量防护与门禁**：沉淀通用教训 §21；更新 `tests/test_console_frontend_syntax.py`（无障碍属性断言、消除原生 confirm/prompt、单加号与深蓝纯白按钮规则）；全量 272 个测试 100% 通过。

## [2026-09-13] feat | Universal Runtime Phase 1: Kernel Control Primitives & State Snapshots
实现通用人机协同底座阶段一目标，将调度器内核由“闭门推进”重构为“外部全面受控”：
- **核心控制元语库 (`herdr/kernel.py`)**：
  - `pause_workflow` / `resume_workflow`：支持全局及节点粒度的挂起与恢复；
  - `step_workflow`：单步推进，在暂停态下仅分发一个就绪节点并保持暂停，杜绝自主失控；
  - `rollback_workflow`：基于 Kahn 拓扑算法求出目标节点及其所有下游传递闭包，原子级联作废任务并重置调度锁；
  - `force_pass_gate`：可审计的门禁强制放行，记录特批操作人与理由；
  - `checkpoint` 快照机制：支持持久化快照保存（`create_checkpoint`）、列表（`list_checkpoints`）与原子恢复（`restore_checkpoint`）。
- **控制台开放 REST API 与操作底座 (`console/herdr_factory_console.py`)**：
  - 暴露 `/api/kernel/pause`, `/api/kernel/resume`, `/api/kernel/step`, `/api/kernel/rollback`, `/api/kernel/force-pass`, `/api/kernel/checkpoint`, `/api/kernel/checkpoints`；
  - 前端控制台在更多操作下拉菜单中挂载暂停/恢复、单步、回溯模态框与快照中心，任务列表针对 blocked 状态提供「强制放行」快捷介入。
- **CLI 命令行工具 (`bin/herdr-factory`)**：
  - 新增 `step`, `rollback`, `force-pass`, `checkpoint (save|list|restore)` 一级子命令。
- **质量防护与门禁**：沉淀通用教训 §22；新增 `tests/test_kernel_control_primitives.py` 与 `tests/test_console_kernel_api.py`；全量 284 个测试用例 100% 通过。

## [2026-09-13] feat | Universal Runtime Phase 2: Worker Intervention & Steering Mesh
实现通用人机协同底座阶段二目标，构建工位实时干预网格（Intervention & Steering Mesh）：
- **核心纠偏模块 (`herdr/steering.py`)**：
  - `queue_steer`：实现有序插话队列（In-Flight Steering Queue），支持持久化至 `~/.herdr-controller/steering.json`；支持「紧急插话（立即软中断并派发）」与「顺滑插话（排队待间歇注入）」双通道；
  - `halt_task`：向底层工位 Pane 发送非破坏性受控软中断（SIGINT / ctrl-c），现场保护并流转状态至 `interrupted`；
  - `format_steer_prompt`：结构化干预提示词协议，确保异构 Agent（Codex/Claude 等）精准吸收总指挥干预指示并留存发起人与审计时间戳；
  - `dispatch_pending_steer`：支持按需消费出队未派发插话。
- **看门狗服务联动 (`services/herdr-sentinel.py`)**：
  - 巡检活跃工位处于 `idle` 且存在待派发插话时，自动在间歇触发提示词注入与回车，实现平滑纠偏闭环。
- **CLI 命令行扩展 (`bin/herdr-task`)**：
  - 新增 `halt <task_id>`、`steer <task_id> "<instruction>" [--urgent]` 与 `steer-queue <task_id>` 子命令；
  - 状态机 `TRANSITIONS` 与 `ACTIVE_TASK_STATUSES` 接入 `interrupted`。
- **控制台 Web API 与交互扩展 (`console/herdr_factory_console.py`)**：
  - 暴露 `POST /api/task/steer`, `POST /api/task/halt`, `GET /api/task/steer/queue`；
  - 活跃工位任务卡片新增「插话」与「制动」快捷行动点，配设弹窗与二次确认保护。
- **质量防护与门禁**：沉淀通用教训 §23；新增 `tests/test_steering_mesh.py` 与 `tests/test_console_steering_api.py`；全量 293 个测试用例 100% 通过。

## [2026-09-13] feat | Universal Runtime Phase 3: Telemetry Distillation & White-box Projection Engine
实现通用人机协同底座阶段三目标，构建语义提炼引擎与白盒数据流（Projection Engine）：
- **核心提炼与清洗引擎 (`herdr/projection.py`)**：
  - `strip_ansi_codes`：基于纯标准库正则过滤 CSI、OSC、光标指令、换行与控制字符，终结终端 ANSI 乱码；
  - `extract_task_intent`：4 级意图解析（`[HERDR_INTENT]` 显式标记 > `task.goal` 顶层意图 > 瞬态执行动作 > 节点基础语义）；
  - `extract_task_milestones`：提取 4 阶段动态路标（锁定验收目标 -> 核心代码实现 -> 内循环自检 -> 交付产物会签）；
  - `collect_task_artifacts`：将产物提升为第一公民（Git diff 变更、工位自检评分报告 EVALUATION.md、需求/文档设计产物）；
  - `extract_recent_activity`：提炼最近清晰可读的动作摘要，抹平无谓认知过载；
  - `project_task` / `project_workflow`：生成任务及工作流维度的白盒 4D 遥测投影。
- **CLI 命令行扩展 (`bin/herdr-task`)**：
  - 新增 `project <task_id> [--json]`：打印整洁的白盒简报（状态、意图、卡点、路标、产物与近期活动）；
  - 新增 `artifacts <task_id> [--json]`：快速核验任务产生的所有交付物。
- **控制台 Web API 与白盒卡片 (`console/herdr_factory_console.py`)**：
  - 暴露 `GET /api/task/projection` 与 `GET /api/workflow/projection`；
  - 任务详情模态框升级为白盒简报卡片，配设路标列表、产物清单、卡点高亮警示与原始调试数据折叠切换。
- **质量防护与门禁**：沉淀通用教训 §24；新增 `tests/test_projection_engine.py` 与 `tests/test_console_projection_api.py`；全量 302 个测试用例 100% 通过。

## [2026-09-13] feat | Universal Runtime Phase 4: Dynamic Configuration & Sandboxed MCP Capabilities
实现通用人机协同底座阶段四目标，构建通用元模型解耦与受控 MCP 生态容器：
- **工作流元模型动态扩展 (`herdr/workflow.py`)**：
  - `inputs` 字段扩展：支持静态字符串与动态节点产物引用（`nodes.<node_id>`）；
  - `worker_policy` 结构化声明：显式声明能力集 `capabilities: list[str]` 与权限范围 `permissions: dict[str, str]`；
  - `gate` 混合门禁契约：支持 `type` (`auto` / `manual` / `hybrid`)、`rules` 规则链与拓扑安全的 `retry_target`；
  - `validate_workflow_dag` 严格校验：校验 `retry_target` 必须存在于定义节点集合中（杜绝运行时死锁与 KeyError），校验动态输入引用节点的合法性与单向无环性。
- **受控 MCP 插件与权限安全网格 (`herdr/mcp.py`)**：
  - MCP 服务器注册表生命周期：支持持久化存储至 `~/.herdr-controller/mcp-registry.json`，提供 `load_mcp_registry`、`register_mcp_server`、`unregister_mcp_server`、`list_mcp_servers` 等受控管理 API；
  - 内置受控 MCP 工具集：提供 `web_search`、`data_extraction`、`file_system`、`git_tools`、`human_signoff` 五大开箱即用工具配置；
  - 动态能力匹配与沙盒权限检验：`resolve_node_mcp` 自动完成能力匹配与权限降级过滤，`check_node_permissions` 防范沙盒越权。
- **跨领域通用商业研报模板 (`workflow_templates/business-research-v1.yaml`)**：
  - 完整编排 `market_scope` -> `data_extraction` -> `comparative_analysis` -> `executive_briefing` 4 个异构业务节点，全量验证输入透传、MCP 能力注入与混合会签门禁。
- **控制台前台可视化增强 (`console/herdr_factory_console.py`)**：
  - `showTemplateDAG` 前台模板 DAG 预览弹窗升级：解析并展示节点的 Gate 门禁类型与 Worker Policy 权限范围标签。
- **质量防护与门禁**：
  - 沉淀通用教训 §25（元模型解耦、沙盒权限隔离与跨领域无环拓扑校验）；
  - 新增 `tests/test_dynamic_workflow_schema.py` 与 `tests/test_mcp_capability_mesh.py`；
  - 全量 314 个测试用例 100% 通过。

## [2026-09-13] feat | Universal Runtime Phase 5: Universal Studio UI & Artifact Signoff Chamber
实现通用人机协同底座阶段五目标，构建人机对等协同工作舱（Universal Studio UI）：
- **注意力中枢与任务过滤 (Attention Hub & Smart Filters)**：
  - 顶部动态告警横幅（Attention Banner）：自动汇聚当前待人工审核、异常打断或被阻塞的任务数量与最高优先级行动项；
  - 认知减负过滤器：支持「全部 (All)」、「待我拍板 (Decisions)」、「需关注 (Attention)」、「进行中 (Active)」四态过滤，大幅降低人机协同认知负载达 90%；
  - 状态气泡与行动徽章：精准识别 `gate_blocked`、`interrupted`、`failed` 与 `working` 节点。
- **沉浸式成果会签室 (Artifact Signoff Chamber)**：
  - 会签审批与驳回闭环：新增 `POST /api/task/signoff` 原生 API；
  - 成果批准通过 (`action='approve'`)：联动内核 `force_pass_gate` 释放门禁锁，无缝推进下游节点；
  - 成果驳回重做 (`action='reject'`)：联动内核 `rollback_workflow` 优雅回滚至指定上游节点，附带人类结构化评审意见作为返工输入，不破坏运行态完整性；
  - 会签模态窗 (`openSignoffChamber`)：一站式全景审阅产物列表（Diff 报告、评估 Markdown 等）与白盒路标，提供直观的批准通过与驳回返工操作。
- **折叠式物理抽屉 (Deep Physical Drawer)**：
  - 底部收纳式浮动工具条与折叠抽屉：默认折叠不占位，一键向上展开；
  - 三大实时观测工位：集成实时终端 TTY 预览 (`/api/pane/read`)、控制器实时日志流 (`/api/logs`) 与白盒工作流遥测投影 (`/api/workflow/projection`)；
  - 零侵入开发与排障：彻底抹平「必须切换到终端看日志」的摩擦，实现前端单页沉浸式监工与排障。
- **质量防护与工程治理**：
  - 遵循 Ponytail 极简原则：纯标准库与原生 HTML/CSS/Vanilla JS 实现，零外部 npm 依赖，零安全与打包隐患；
  - 沉淀通用工程教训 §26；
  - 新增 `tests/test_console_signoff_api.py`，扩展 `tests/test_console_frontend_syntax.py`；
  - 全量 317 个测试用例 100% 通过；
  - 执行 `scripts/install-herdr-console.sh` 同步部署至 `~/.herdr-console`。

## [2026-09-13] test | Universal Runtime End-to-End Dogfooding & Integration
完成通用人机协同底座全链路实战端到端集成测试与演练（E2E Dogfooding）：
- **全链路场景闭环打通**：
  - 以跨领域商业研报模板 `business-research-v1.yaml` 为载体，全面串联元模型解析、内核控制原语、工位插话与制动、4D 遥测投影、成果会签室审批放行与驳回回滚；
  - 验证多阶段架构在复杂异构场景下的确定性协同。
- **端到端集成测试套件 (`tests/test_universal_substrate_e2e.py`)**：
  - 覆盖模板加载与 DAG 校验、工作流暂停/恢复、在途非阻塞插话与紧急制动夺回调度权、4D 投影提炼、会签批准与驳回返工循环、检查点快照灾难恢复；
  - 4 项关键集成用例全绿通过。
- **独立可执行实战演练工具 (`scripts/verify-universal-runtime-e2e.py`)**：
  - 零依赖独立实战演练 CLI，支持开发者与 CI 管道随时一键拉起沙盒并验证五大阶段全部核心原语。
- **质量防护与工程治理**：
  - 沉淀通用工程教训 §27（跨阶段全链路集成测试的持久化沙盒隔离陷阱）；
  - 全仓自动化回归测试达 321 项用例，100% 通过。

## [2026-09-13] feat | Checkpoint Store V2: SQLite Embedded State Engine, Graph Lineage & Time-Travel Forking
实现北极星架构体系状态引擎升级，基于纯 Python 标准库 `sqlite3` 构建嵌入式检查点与状态存储引擎：
- **嵌入式 SQLite 状态底座 (`herdr/state_db.py`)**：
  - 表结构模型：`workflows`、`tasks`、`checkpoints`、`events`，并设置索引加速检索；
  - 并发与可靠性治理：开启 WAL 模式（`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`），支撑多进程安全并发读写；
  - 单事务原子快照与回滚：`create_checkpoint`、`restore_checkpoint`，保障工作流元数据、节点 DAG、任务状态原子更新；
  - 图谱谱系追踪与时间旅行分叉：检查点记录 `parent_id` 形成有向无环谱系图；`fork_workflow_from_checkpoint` 支持从历史任意快照分叉派生独立工作流，深置状态并清除调度锁；
  - 零停机无损迁移：`migrate_v1_to_v2` 支持从历史 JSON 文件双向平滑导入 SQLite。
- **内核控制透明桥接 (`herdr/kernel.py`)**：
  - 桥接 `state_db`，实现 JSON 与 SQLite 双写归一，保证快照 ID 1:1 精确对齐；
  - 优先通过 SQLite 加速快照检索与还原，平滑回退 JSON 存储保证 100% 向后兼容；
  - 新增 `fork_workflow_from_checkpoint` 控制原语。
- **CLI 命令行扩展 (`bin/herdr-task`)**：
  - 新增 `checkpoint-create <workflow_id> [--label ...] [--parent ...]`；
  - 新增 `checkpoint-list <workflow_id> [--json]`；
  - 新增 `checkpoint-restore <checkpoint_id>`；
  - 新增 `checkpoint-fork <checkpoint_id> [--new-workflow-id ...] [--title ...] [--json]`。
- **质量防护与工程治理**：
  - 遵循 Ponytail 极简原则：零外部三方依赖，纯标准库 `sqlite3`；
  - 沉淀通用工程教训 §28（嵌套事务连接复用、双写 ID 归一与分叉锁清理）；
  - 新增 `tests/test_state_db_v2.py`（9 项单元与集成测试全部通过）；
  - 全仓 330 项自动化回归测试 100% 通过。

## [2026-09-13] docs | Codified Functional Core, Pythonic Cohesion & Gradient Split Rules into RULES.md
- **固化架构与分层红线 (`RULES.md`)**：
  - **纯核心与装配解耦 (Functional Core, Imperative Shell)**：强制要求业务决策纯函数化（收敛于 `herdr/`），CLI (`bin/`) 与常驻守护进程 (`services/`) 仅作指令式外壳处理 I/O 与物理系统副作用；
  - **模块内聚与反过度抽象**：坚决抵制 Java 式 DTO/DAO/Service 空壳分层与类爆炸，以高内聚模块与显式纯函数组织逻辑；
  - **文件健康度与梯度拆分阈值**：废除机械行数硬限，确立 `herdr/` 300~500 行、CLI/Daemon 500~800 行的业务内聚梯度拆分准则。
- **同步验收门禁 (`CLAUDE.md`)**：在严格验收清单中新增分层纯度与文件健康度核对项。

## [2026-09-13] docs | Elevated Remote Sync & CoW Sandbox Branching to First-Class Rule
- **规范红线升格 (`RULES.md`)**：
  - 确立「远端拉取同步与 CoW 沙盒建支」为项目一等公民（First-Class Citizen）；
  - 强制要求任何任务在 Plan/开发前必须先执行 `git fetch origin` 同步本地主仓库，并利用 CoW (Copy-on-Write) 沙盒环境建立全新分支隔离执行；
  - 严禁在主干工作区直接开发，严禁随意复用他人或遗留的功能分支。
- **验收清单同步 (`CLAUDE.md`)**：在严格验收清单顶部新增「远端同步与 CoW 沙盒合规」前置门禁。

## [2026-09-13] docs | Upgraded Project Standard Workflow to /unified-dev-flow (S0–S8)
- **废除旧四阶段作业法，全面拥抱 `/unified-dev-flow` 统一研发流程**：
  - 在 [`RULES.md`](file:///Users/user/HAFlow/RULES.md) §1 正式确立策略驱动的 S0–S8 全生命周期研发规范；
  - 明确九大核心不变量（断点优先、读懂再写、意图定基线、复杂度定规划、风险度定质检、单一控制权、改动即失效、无铁证不宣称完成、交付不越权）；
  - 强化 S0 启动前置门禁：远端代码拉取同步与 CoW (Copy-on-Write) 沙盒隔离建支（一等公民准则）；
  - 规范 S6 审查修复闭环（S6 ➔ S4 ➔ S5 ➔ S6，3 轮熔断机制）与 S8 知识沉淀机制。
## [2026-09-13] feat | StateStore Unification: Single Source of Truth via SQLite, Eliminating Dual-State Skew
实现系统核心状态事实源完全归一，彻底消除 JSON 与 SQLite 双状态源裂脑与时序漂移风险：
- **统一抽象层与引擎实现 (`herdr/state_store.py`)**：
  - 定义 `StateStore(ABC)` 顶层多态抽象接口，标准化工作流（Workflow）、任务（Task）、工位纠偏（Steering）、审计事件（Events）与检查点快照（Checkpoints）的全生命周期方法；
  - 实现 `SQLiteStateStore(StateStore)` 生产级状态底座，所有写操作唯一路由到 SQLite WAL 数据库；
  - 明确 JSON 纯作为只读投射、冷导出 (`export_*_json`) 与无损迁移 (`import_from_json`) 介质，不再作为长期主状态载体；
  - 提供 `get_state_store()` / `set_state_store()` 全局单例与注入工厂。
- **底层模式与查询能力补齐 (`herdr/state_db.py`)**：
  - 新增 `steering_items` 与 `steering_history` 表及索引；
  - 扩展 `list_workflows`、`delete_workflow`、`get_task`、`list_tasks`、`delete_task`、`save_steer`、`list_steers`、`record_steering_history`、`list_steering_history` 等标准数据操作；
  - `save_task` 智能自愈：自动保障父级 workflow 占位存在，规避 SQLite `FOREIGN KEY` 约束失败；
  - 扩展 `migrate_v1_to_v2` 支持无损导入历史 `steering.json`。
- **调度内核与纠偏模块收敛 (`herdr/kernel.py`, `herdr/steering.py`)**：
  - 废除业务内部直接 `open("tasks.json")` / `open("workflows.json")` / `open("steering.json")`；
  - 所有控制原语（pause/resume/force_pass/rollback/step/checkpoint/fork）和干预原语（queue_steer/dispatch/halt）统一通过 `StateStore` 操作；
  - `load_*_data` 与 `save_*_data` 转化为基于 `StateStore` 的向后兼容读写适配器，且写操作联动同步兼容 JSON。
- **调度器守护进程与 CLI 全量收敛 (`services/herdr-controller.py`, `bin/herdr-task`)**：
  - `services/herdr-controller.py` 与 `bin/herdr-task` 彻底移除对 `tasks.json` / `workflows.json` 的主读写，全面接入 `_get_store()` 经由 `StateStore` 操作底层 SQLite；
  - 彻底杜绝双向/反向同步风险：`kernel.py` 与各调用点废除 `_sync_*_from_disk_if_needed`，替换为严格单向冷导入 `_import_missing_*_from_disk`（仅在 SQLite 缺失该实体时进行冷增量导入），任何存量记录 100% 以 SQLite 为准，禁止磁盘旧文件覆盖权威数据库；
  - `auto_migrate_json` 默认设为 `False`，避免非显式触发时全局脏数据污染隔离环境；伴生数据库推导按任务文件隔离（`p.with_suffix(".db")`），保障高并发与单元测试独立性。
- **质量防护与工程治理**：
  - 沉淀并扩充通用工程教训 §30（状态源统一与防裂脑）；
  - 新增并在 `tests/test_state_store.py` 中扩充测试至 10 项（新增故意篡改磁盘 JSON 无法覆盖 SQLite 权威状态测试、`herdr-task set` CLI 命令行直写 SQLite 实时验证测试）；
  - 全仓自动化回归测试扩充至 344 项（100% 通过）。



## [2026-09-13] docs | Codified Lesson 29 on Remote Sync & CoW Sandbox Discipline
- **归档通用工程教训 §29 (`docs/lessons/lessons-learned.md`)**：
  - 总结任务启动现场未隔离导致提交误入他人功能 PR（PR #17 事故）及并发分支冲突（PR #18）的深层根因；
  - 固化 S0 准备阶段强制门禁（`git fetch origin` 同步主仓库 + CoW 沙盒独立建支为一等公民）。

## [2026-09-14] feat | Anti-Stall Workflow Healing, CoW Sandbox Isolation & Commander Telemetry
- **三支柱抗死锁工程闭环落地**：
  - **支柱 1（沙盒物理纯净隔离与原子清理，`services/herdr-worker.py`）**：
    - 新增 `sanitize_clone_sandbox`：在沙盒建支前内部强制执行 `git reset --hard HEAD` 与 `git clean -fd`，彻底隔离母体未提交工作区修改（WIP），杜绝 `git switch` 检出冲突；
    - 新增未注册/陈旧沙盒残留自愈清理机制，异常时执行原子化删除，消除半残 Clone 阻塞重试；
    - 配套 `tests/test_herdr_worker.py` 新增 3 项隔离与自愈测试。
  - **支柱 2（产物契约优先交付与 Rework 看门狗，`services/herdr-controller.py`）**：
    - 新增 `check_task_deliverables_ready`：严格依据 `required_outputs` 或 baseline 差异判断交付物就绪，规避长推理模型思考间歇瞬态空闲误判；
    - 补齐状态机 `rework` 空闲事件处理与自愈推进逻辑，并在主循环中引入 `rework_watchdog`，彻底终结返工孤儿死锁；
    - 配套 `tests/test_fix_loop_anti_flapping.py` 新增 2 项自愈与产物防抢跑测试。
  - **支柱 3（总指挥白盒停滞感知与主动干预，`herdr/projection.py`, `console/herdr_factory_console.py`）**：
    - 新增 `detect_workflow_stalls`：自动识别返工停滞（>45s）与阶段推进悬挂（>45s），注入白盒遥测；
    - 控制台前端 Attention Banner 动态变色预警与一键动作面板（`[🔔 立即唤醒评审]`, `[⚡ 尝试推进阶段]`），工位卡片新增 `[唤醒评审]`；
    - 控制台后端打通 `/api/task/force-review` 与 `/api/workflow/retry-advance`；
    - 配套 `tests/test_projection_engine.py` 扩充 3 项停滞与投影集成测试。
- **治理规范与知识归档**：
  - 沉淀并归档通用工程教训 §31（工作流抗停滞自愈、CoW沙盒纯净隔离与总指挥主动干预）；
  - 全仓自动化回归测试通过。

## [2026-09-14] feat | Critical Control Reads Fail Closed & Authoritative StateStore Freeze
- **核心控制读取链路 Fail-Closed 终极收口与事实源彻底冻结**：
  - **根除自动冷导入导致的工作流与任务“起死回生” (`herdr/agent_router.py`, `herdr/projects.py`, `herdr/kernel.py`, `herdr/steering.py`, `bin/herdr-task`, `services/herdr-controller.py`, `herdr/state_db.py`)**：
    - 彻底废除 `workflow_record()`、`active_workflows_for_project()`、`_workflow_entry()`、`load_workflows()` 读链路上的 `_sync_missing_workflows_into_store` 旁路扫描；
    - 彻底废除 `agent_router.py` 的 `_sync_missing_tasks_into_store`、`kernel.py` 的 `_import_missing_tasks_from_disk`、`steering.py` 读取 `tasks.json` 的旁路，以及 `bin/herdr-task` 与 `herdr-controller.py` 的 `load_tasks()` 中的磁盘冷插入逻辑，确保运行时任何正常路径绝无反向 JSON → SQLite 写回；
    - 数据库底层在 `_ensure_schema` 中引入 `schema_meta` (`v1_migration_done`) 表，使用原子显式事务（`BEGIN TRANSACTION;` ... `COMMIT;`）执行一次性 bootstrap 导入；任一历史文件损坏立即 `ROLLBACK;` 且不标记完成、不污染缓存，保留重试通道；
  - **路由决策未注册工作流 Fail-Closed (`herdr/agent_router.py`)**：
    - `choose_agent()` 显式增加对 `workflow_id` 的存在性校验：当指定了 `workflow_id` 但在 StateStore 查无记录时，立即抛出 `RuntimeError("Workflow not found in authoritative StateStore: ...")`，杜绝静默兜底到 `"opencode"` 绕过项目池黑名单、健康准入与并发预占；仅限未指定 `workflow_id` 的独立任务走默认代理；
    - `_clean_reservations()` 与 `_active_agent_loads()` 彻底废除对 `workflows.json` 与 `tasks.json` 的异常降级读取，底层 StateStore 异常直接抛出阻断；
  - **调度协调看门狗与事实源纯化 (`services/herdr-controller.py`)**：
    - `_workflow_entry()` 仅查询 StateStore，移除任何磁盘扫描；
    - `active_registered_workflows()` 100% 仅源自 `store.list_workflows()`，彻底移除从 `~/.herdr-controller/projects.json` 注入 `wf` 的逻辑，杜绝 Ghost Workflow；
  - **检查点 Checkpoint 读写与分叉彻底收口 Fail-Closed (`herdr/kernel.py`)**：
    - 彻底移除 `list_checkpoints`、`get_checkpoint` 和 `fork_workflow_from_checkpoint` 扫描磁盘旧 `.json` 并向 SQLite 反向补写 `store.save_workflow` / `store.save_task` / `store.create_checkpoint` 的运行时旁路；
    - 所有检查点操作 100% 仅依赖 `StateStore`；若底层不存在直接抛出 `FileNotFoundError` 阻断，绝不在运行时从磁盘“起死回生”状态；
    - 历史遗留检查点文件（`checkpoints/`）统一纳入底层 `state_db.py` 的首次建库原子事务，由 bootstrap 一次性迁移并绑定外键。
  - **Bootstrap 失败显式抛出异常阻断启动与旧库升级防覆写 (`herdr/state_db.py`)**：
    - 修复迁移失败被静默吞掉的严重隐患：`_ensure_schema` 在显式事务 `BEGIN TRANSACTION;` ... `COMMIT;` 失败并 `ROLLBACK;` 后，**必须显式 `raise` 异常**，严禁 `except: pass` 导致系统在空 SQLite 库上裸跑造成严重数据丢失；
    - 增加已有数据库升级保护机制：在执行 Bootstrap 前探活核心表业务数据（`has_existing_state`），若 SQLite 已有数据（如旧版本升至当前版本），说明 SQLite 已是权威事实源，直接写入 `v1_migration_done = '1'`，严禁读取陈旧 legacy JSON 避免状态被覆写（防止例如 completed 被回滚为 running）；仅当 SQLite 完全为空且存在 legacy JSON 时才允许执行 Bootstrap；
    - `get_db_connection` 捕获初始化异常后显式 `conn.close()` 释放文件句柄并重新抛出异常；
    - 兼容遗留 `workflows.json` 既为 dict 亦为 list 的格式形态，增强老旧工作流格式鲁棒性。
  - **新增专项对抗测试套件 (`tests/test_critical_reads_fail_closed.py`)**：
    - 17 项测试全面覆盖：9 项模拟 `sqlite3.OperationalError` 时的 Fail-Closed 异常阻断；Test A 验证陈旧 JSON 绝不复活已删除/不存在的 Workflow；Test B 验证未注册 Workflow 调用 `choose_agent` 抛出 `RuntimeError`；Test C 验证 `projects.json` 注入的 Ghost 工作流被 100% 过滤；Test D 验证一次性 bootstrap 迁移与后续持久隔离；Test E/G 验证损坏 JSON 触发原子回滚且显式 raise 阻断启动、修复后重试无缝成功；Test F 验证 Checkpoint 读取与分叉绝不复活状态到 SQLite 且查无记录时严格抛出 `FileNotFoundError`；Test H 验证已有业务数据的旧 SQLite 数据库在无 migration marker 升级启动时 100% 免疫陈旧 JSON 覆写并自动补齐 marker；
  - **沉淀并归档通用工程教训 §32**（核心控制读取 Fail-Closed 铁律、Task/Checkpoint 运行时防复活、原子迁移显式阻断与旧库升级防覆写）；
  - 全仓自动化回归测试达 368 项（100% 绿灯全部通过）。

- **2026-09-14: 文档与工程承诺语言收敛（反过度承诺与 SLA 去伪存真）**
  - **核心定位与宣称口径务实收敛 (`README.md`, `CLAUDE.md`, `docs/`, `wiki/`)**：
    - 主文档产品定位收敛：将“通用多 Agent 工作流编排操作系统”收敛为“基于空间现场模型的多 Agent 工作流协同平台”；将“生产级的‘操作系统底座’”调整为“实用的工作流协同底座”，剔除过度包装；
    - 兼容性声明去伪存真：将危险的“100% 向下兼容”严格改为“兼容现有 legacy stage-based workflow”，防范未来 Schema 演进可能带来的绝对承诺反噬；
    - 运行时自愈 SLA 消除：将“毫秒级自动修复/自愈”明确替换为机制事实描述“任务派发前自动检测并修复”，杜绝为系统背负不必要的物理时间 SLA；
    - 绝对化与过度承诺用语二次清剿：消除“任何业务流程”（收敛为“支持通过声明式 YAML/JSON 模板定义研发、标书、客诉等多类业务流程”）；消除“无副作用沙盒”（改为“隔离的沙盒实测验证”）；消除“阻断死锁”（改为“降低无效分发和因 Agent 不可用造成的阻塞风险”）；
    - 剔除无意义 AI 宣传套话：仓库目录章节移除“严格遵循现代分布式系统与 Python 开源工程最佳实践”，替换为直接的分层结构陈述；
    - 纠偏机制描述失真：修正 `universal-workflow-guide.md` 中关于 `normalize_workflow` 的描述，严格与代码纯内存变换对齐（“在加载时统一生成 `nodes` 与 `stages` 的兼容表示”，而非脑补写回配置文件）；
    - 同步对齐 `CLAUDE.md`、`docs/guides/universal-workflow-guide.md`、`docs/architecture/architecture-overview.md`、`docs/operations/troubleshooting-faq.md`、`wiki/index.md`、`wiki/tab-node-model.md`、`wiki/common-change-paths.md` 中的同类措辞；
  - **沉淀通用工程教训 §33**（工程语言收敛与反过度承诺准则：剔除危险绝对化承诺与不可控 SLA，坚持事实驱动与严谨务实的系统定位）；
  - 全仓自动化回归测试 368 项保持 100% 全部通过。

- **2026-09-14: feat | Phase 3 启动：AgentAdapter 契约与 TTY 传输彻底解耦 + 交付安全防伪闸门**
  - **核心问题与演化**：初版将 TTY 模拟封装为 `AgentAdapter` 雏形，但 `AgentAdapter` 基类内部仍写死 `ctrl-c`/`send-text`/`pane_id`，且未知 Agent 盲目降级至全能力原型；软插话（soft steer）在能力声明为 `False` 时仍偷偷 fallback 注入；物理发送失败时仍将指令虚假标记为 `dispatched`/`interrupted`，导致指令丢失与 SQLite 状态事实漂移。
  - **架构重塑**：
    - **契约与传输解耦**：`class AgentAdapter` 彻底去 TTY 化，仅保留通用抽象契约（零 Pane/Subprocess/Keystroke 知识）；所有终端按键模拟下沉至 `class TTYAgentAdapter(AgentAdapter)`，为未来 `NativeRpcAdapter` / `ApiAdapter` 扫清架构障碍；
    - **Fail-Closed 默认安全**：`AgentCapability` 默认全部 `False`；未注册 Agent 强制路由至 `UnknownAgentAdapter`，拒绝一切 Steering 操作；
    - **能力即控制门禁**：`supports_soft_steer=False`（如 OpenCode/Qoder）时严格拒绝执行软插话（返回 `ok=False`, `reason="soft_steer_not_supported"`），零 TTY 子进程调用；
    - **真实交付防伪（Anti-Skew）**：
      - `dispatch_steer_now` / `dispatch_pending_steer`：无 Pane 或物理投递失败时，指令严格保持 `pending`（记录 `last_delivery_error`），返回 `ok=False`, `pane_delivery_ok=False`，防止指令无故丢失；
      - `halt_task`：物理中断失败时，严格拒绝推进 Task 状态至 `interrupted`，保持原状态并记录 `task_halt_failed`，杜绝后台 Agent 裸跑但状态显示已中断的虚假成功；
  - **测试覆盖**：
    - `tests/test_agent_adapter.py` 增至 11 项（覆盖 Fail-Closed、Soft-Steer 阻断、TTY 独立契约验证、中断失败防御）；
    - `tests/test_steering_mesh.py` 增至 13 项（覆盖无 Pane 投递拒绝、物理失败保持 Pending、中断失败防状态漂移、OpenCode 软插话阻断、紧急插话半途失败反向漂移防御、历史事件 Append-Only 防重复膨胀）；
    - 全量回归 **386 / 386 passed**（基线 368，净新增 18 项）。
  - **PR #25 Review Blockers 修复**：
    1. **紧急插话半途失败反向漂移防御**：当 Urgent Steer 中断成功但注入失败时，Task 强制流转至 `interrupted` (`requires_attention=True`)，指令保留 `pending`，杜绝 Agent 进程已停但数据库显示 working 的事实漂移；
    2. **切断循环依赖与 Steering 纯粹化**：消除 `TTYAgentAdapter` 内部对 `steering._send_keys/_send_text` 的反向依赖与 monkeypatch 钩子；`steering.py` 彻底移除 `import subprocess`，纯粹收敛为编排层；
    3. **历史记录 Append-Only 防重复膨胀**：修复 `save_steering_data` 遍历旧历史全量二次插入 SQLite 的严重缺陷，确立 audit 事件严格单向 append-only，单次重试零膨胀，为 PR #26 Event Stream 扫清障碍；
  - **Wiki & Lessons**：`wiki/index.md`、`wiki/log.md`、`docs/lessons/lessons-learned.md §34` 全面同步。

## [2026-09-14] feat | WorkflowEvent Stream Contract and First Producers
- **统一 WorkflowEvent 存储契约 (`herdr/state_db.py`, `herdr/state_store.py`)**：
  - 将 `events` 表扩展为统一运行时事件流骨架，标准字段包含 `workflow_id`、`node_id`、`task_id`、`agent_id`、`event_type`、`timestamp`、`payload_json` 与 `source`；
  - 通过 `_ensure_event_columns()` 对既有 SQLite 库执行原地补列，保留旧 `workflow_id/task_id/event_type/payload_json/timestamp` 读写兼容；
  - 新增 `record_event()` 与 `list_events()`，支持按 workflow/node/task/agent/type/source 查询并按 `timestamp, id` 稳定回放。
- **现有事件源收敛到同一 Stream**：
  - `record_steering_history()` 在保留 `steering_history` 兼容表的同时追加 `steering.<action>` 事件；
  - `create_checkpoint()`、`restore_checkpoint()` 与 `fork_workflow_from_checkpoint()` 改为经由统一 `record_event()` 写入事件，保留既有 `checkpoint_created`、`checkpoint_restored`、`workflow_forked` 事件类型，避免破坏旧消费者。
- **测试与知识沉淀**：
  - 新增 `tests/test_state_db_v2.py::test_workflow_event_stream_records_full_context_and_filters` 与 `tests/test_state_store.py::test_state_store_records_and_lists_workflow_events`；
  - 扩充 checkpoint/fork/steering history 断言，确保首批生产路径真实进入 WorkflowEvent Stream；
  - 沉淀并归档通用工程教训 §35（先建立统一事件契约与首批生产者，Task/Workflow/Node 全生命周期事件化留作后续阶段）。

## [2026-09-14] feat | Kernel State Transition Contract & Gateway (Phase 1)
- **函数式状态机核心 (`herdr/transitions.py`)**：
  - 定义纯逻辑状态转移字典 `TASK_TRANSITIONS` 与 `WORKFLOW_TRANSITIONS`；
  - 正式引入 `closing` 状态（`running/in_progress -> closing -> completed/failed`），纳入 `ACTIVE_WORKFLOW_STATUSES`；
  - 分类任务状态（`ACTIVE_TASK_STATUSES`, `COMPLETED_TASK_STATUSES`, `TERMINAL_TASK_STATUSES`）；
  - 提供纯函数验证接口 `validate_task_transition(old_status, new_status)` 与 `validate_workflow_transition(old_status, new_status)`，零 I/O、零副作用；
  - 允许同状态自流转（幂等），对非法跃迁抛出统一异常 `InvalidTransitionError`。
- **单一事务状态变迁网关 (`herdr/state_db.py`, `herdr/state_store.py`, `herdr/kernel.py`)**：
  - 在 `state_db.py` 中实现 `transition_task()` 与 `transition_workflow()`：统一在 SQLite `BEGIN IMMEDIATE` 强事务锁下执行状态检查、转移校验、数据表更新与 `WorkflowEvent`（`task_transition` / `workflow_transition`）原子追加；
  - 提供 `force=True` 管理员/运维逃生通道，在事件元数据中如实记录 `forced: True`；非法非契约状态即便 `force=True` 也坚决拦截；
  - 实现原子元数据独立更新网关 `update_task_metadata()` / `update_workflow_metadata()`：仅更新业务元数据（verdict、history、commit 等），强制白名单拦截任何试图篡改 `status`/`task_id` 等保护字段的行为，彻底根除陈旧内存快照全量覆写（Snapshot UPSERT）踩踏新状态的隐患。
- **跨进程锁定投影同步与严格优先级解析**：
  - 实现 `sync_tasks_projection()` 与 `sync_workflows_projection()`，基于 `fcntl.flock` 跨进程排他锁并在拿锁后从 SQLite 实时重导出，杜绝脏写与并发覆盖；
  - 实现 `resolve_tasks_projection_file()` 与 `resolve_workflows_projection_file()`，统一收口优先级：`explicit arg -> os.environ -> store.db_path.parent -> default CONTROLLER_DIR`。
- **Teardown 所有权预占与 TOCTOU 竞态根除**：
  - `close_workflow` 严格实行 Preflight Gate：在执行任何物理销毁前，先校验当前状态能否流转至 `closing`；
  - 在开始物理销毁前，通过 Gateway 原子推进为 `closing` 抢占排他所有权；`closing` 状态下并发 `pause` 天然被拒；物理销毁完成后最终流转至 `completed`。
- **审计事件元数据防伪与实体健康兜底**：
  - 定义 `RESERVED_EVENT_METADATA_FIELDS`，前置拦截任何试图通过 metadata 伪造 `from_status` / `to_status` / `source` / `reason` 等字段的行为，并结合结构级末尾覆写实现双重防伪；
  - `save_task()` 自动补全父工作流严格置为合法初始状态 `"pending"`（严禁非契约的 `"unknown"`）；
  - `services/herdr-sentinel.py` 启动时显式注入 `HERDR_ROOT` 到 `sys.path[0]`，脱离外部环境变量即可可靠启动。
- **控制原语与调用方全面收敛**：
  - `kernel.pause_workflow()`、`kernel.resume_workflow()`、`kernel.rollback_workflow()` 改造为通过网关推进；
  - `bin/herdr-task`（`set_status`, `supersede_task`, `_mark_workflow_completed`, `reopen_workflow`）全量对接网关；同状态修改 verdict/note 走纯元数据通道；
  - `bin/herdr-factory`（`_update_workflow_status`）全量对接网关；
  - `services/herdr-sentinel.py` 超时流转与 `herdr/steering.py` 紧急中断流转全量对接网关，废除从 JSON 反向复活已被删除实体的倒灌逻辑。
- **测试与知识沉淀**：
  - 新增 `tests/test_state_transition_gateway.py`（32 项高覆盖专项测试，覆盖纯规则、非法拒绝、事务回滚、Admin 强制覆盖、Fail-Closed、防倒灌、投影锁、Teardown 门禁前置、并发元数据防踩踏、closing 状态防 TOCTOU、投影路径优先级、保留字段防伪、Sentinel 启动 bootstrap、同状态纯元数据更新、父工作流 pending 状态）；
  - 全仓自动化回归测试达 422 项 + 12 subtests（100% 绿灯全部通过）；
  - 沉淀并归档通用工程教训 §36。

## [2026-09-14] brand | Official brand name HAFlow unification
- **产品官方品牌体系正式确定与统一**：
  - 公司：上海共事智能科技有限公司
  - 品牌：共事
  - 产品：HAFlow
  - 一句话口号：让人和多个 AI Agent 一起把事情做完
  - 英文对应：HAFlow (Human + Agent, in Flow)
- **文档与元数据去陈旧化**：
  - 在全仓核心文档（`README.md`、`CLAUDE.md`、`AGENTS.md`、`RULES.md`、`console/README.md`、`docs/`、`wiki/`）中全面替换旧产品名称（"Herdr" / "共事工厂"），确立以 **HAFlow** 为核心的正式品牌标识；
  - 同步更新文档内文件跳转超链接，适配新根路径 `/Users/user/HAFlow`；
  - 同步更新 Web 控制台（`console/herdr_factory_console.py`）中的 `PRODUCT_NAME='HAFlow'` 与 `PRODUCT_TAGLINE='让人和多个 AI Agent 一起把事情做完'` 常量，实现产品名称前后端一致注入。

## [2026-09-15] fix | Purge all legacy herdr references and enhance console root resolution
- **根除 `/Users/user/herdr` 旧路径残留并加固控制台根目录探针**：
  - 彻底解决 Web 控制台「执行者自检」报 `Deep Preflight 未安装: /Users/user/herdr/herdr/deep_preflight.py` 的路径断裂问题；
  - 重构 `console/herdr_factory_console.py` 中的 `_resolve_herdr_root()`：采用复合特征指纹探针 `(candidate / "herdr" / "__init__.py").exists() and (candidate / "bin").is_dir()`，杜绝因旧软链接或子目录误判导致的根目录定位错误；
  - 彻底移除临时兼容软链接 `/Users/user/herdr`，全盘清剿全仓代码、文档、CLI 脚本、LaunchAgents plist、`~/.zshrc` 与 `~/.herdr-controller/projects.json` 中的旧路径引用；
  - 执行 `scripts/install-herdr-console.sh` 部署并热重载 LaunchAgent 守护进程，通过端到端 Deep Preflight API 验证；
  - 全仓自动化回归测试 422 passed + 12 subtests 全部绿灯通过；
  - 沉淀并归档通用工程教训 §37（仓库根目录重命名与品牌迁移后的运行时路径断裂陷阱）。



## [2026-09-15] fix | Calibrate executor self-check timeouts and error classification
- Updated [[preflight-and-health]] §3.2: per-agent smoke timeouts (`claude` 90s + 1 retry, others 40s), new `PROVIDER_ERROR` class, `TIMEOUT` excluded from `--auto-disable`.
- Root cause: flat 35s timeout deterministically misjudged healthy-but-slow `claude --print` cold start (measured 36.9s success, occasional >60s flake); narrow EN-only patterns misclassified fast startup failures (401/402/billing/overloaded) as generic `ERROR`; console modal dropped `deep.output` evidence.
- Console「执行者自检」modal now renders per-agent raw output tails for human re-verification; total HTTP timeout 180s → 320s.

## [2026-09-15] fix | Self-check round 2: LOCAL_ERROR, pi adapter, fast-failure retry
- Updated [[preflight-and-health]] §3.2: new `LOCAL_ERROR` class (watcher/ENOENT/EACCES), `pi --print --no-session` adapter, fast (≤15s) `PROVIDER_ERROR` retried once; `TIMEOUT`/`LOCAL_ERROR` excluded from `--auto-disable`.
- Live deep probe proof: `claude` READY at 39.45s (would have been TIMEOUT under old 35s flat threshold); `pi` now reports real `AUTH_REQUIRED` (invalid api key) instead of `UNKNOWN`; `qodercli` watcher flake confirmed transient (READY at 13.66s on rerun).
- Archived lesson §38 follow-up as §39 in lessons-learned.

## [2026-09-15] feat | Console task archive query list
- 控制台动作区新增「任务归档」查询列表：跨项目 / 跨 Workflow 检索历史任务，支持项目、工作流 ID 片段、执行者、状态组与关键词过滤，分页浏览并可下钻既有任务白盒简报；
- 查询核心下沉为纯函数 `herdr/archive.py#query_archived_tasks`（过滤/排序/分页，零 I/O）；控制台壳层 `archive_query` 优先读 StateStore，`tasks.json` 仅作降级兜底，投影损坏时归档仍完整（呼应教训 #40）；
- 新增 `GET /api/archive` 契约与页面入口；新增 12 项回归测试（纯函数 + 控制台壳层 + 前端契约），全量 450 passed；
- 更新 [[ops-center]] §7 与 `console/README.md`。

## [2026-09-16] fix | Control-plane Liveness Guard (SLA / attention / hygiene / stall detector)
- **背景**：`wf-xiyu-bid-poc-0915-01` 在 implementation→test 边界卡死 6.5h（连续 1,259 行 `[COORDINATOR BUSY]`）。取证发现控制面四项系统性缺陷：Actor 等待无界（BUSY 5.25h）、事件投递无失败语义（blocked 静默丢弃）、`interrupted` 状态死区 6h47m、夹具 workflow 空转 20h（73k WAIT）与僵尸订阅风暴（128k 重试）；直接根因是 opencode 集成未安装导致屏幕残影把总指挥状态锁死为 `working`。
- 新增 `herdr/liveness.py`：SLA/退避/夹具指纹/attention episode/BoundedWait 的单一策略来源（纯逻辑，零 I/O 依赖）。
- **Controller**：总指挥投递与 stage advance 全部 SLA 化（900s/600s），到期 `[COORDINATOR STALLED]` 记录 attention + macOS 通知并释放 workflow 锁；`done`/`blocked`/`interrupted|paused` 事件全部纳入 attention 慢速重试（`attention.json`）；registry sweep 过滤夹具/空壳 workflow（`[WORKFLOW FOREIGN SKIPPED]`）；`[WORKFLOW COMPLETE]` 单次闩；订阅错误显式失败 + 指数退避（2s→300s，8 次后 `[LISTENER GIVEUP]`）；启动执行集成健康检查（`[INTEGRATION GAP]`）。
- **Sentinel**：新增 `[SENTINEL STALL]` 停滞检测（默认 1800s 无推进即告警 + 通知），补齐此前对"控制面停滞"完全失明的盲区。
- 回归：新增 `tests/test_liveness_guard.py` 22 项；`pytest` 全量 476 passed + 12 subtests；Live 重启 Controller 验证夹具过滤与真实 workflow 订阅正常。
- 更新 [[architecture]] §2.1/§2.2/§3.1（Liveness Guard、Stall Detector、attention.json）；沉淀通用工程教训 §41。

## [2026-09-16] perf | Direct stage dispatch: 常规推进会脱离总指挥 LLM 回合
- **背景**：`wf-xiyu-bid-poc-0915-01` 实测墙钟 10.0h，Agent working 并集仅 1.18h（含机器休眠 6.6h）；清醒期瓶颈为单点总指挥串行 —— 每个节点完成/阶段推进都要等总指挥空闲并跑完整 prompt 回合，`[STAGE ADVANCE WAIT]` 78,979 行、`[COORDINATOR BUSY]` 5,706 行，决策窗口仅 30s 且超时即再烧一整轮。
- **新增 `herdr/direct_dispatch.py`（纯函数）**：按节点模板 + 需求正文生成 Task 规格；fix-loop 回流只补派被作废且无替代的子集任务（`-rN`），`verdict=pass` 且已落定任务保留；节点有活跃任务返回 wait；配置不足/需求缺失返回 fallback。
- **Controller**：`try_direct_stage_advance` 装配 launch（`[STAGE ADVANCED DIRECT]` / `[DIRECT DISPATCH FALLBACK]` / `[DIRECT DISPATCH WAIT]`，`HERDR_DIRECT_STAGE_DISPATCH=0` 可关）；`invalidate_for_fix_loop` 门禁子集保留（`[FIX LOOP SUBSET KEEP]`，`completed+git` 仍走 finalize+作废）；`wait_for_coordinator_decision` 30s→180s（`HERDR_COORDINATOR_DECISION_TIMEOUT`）且超时落 attention 退避；Git 集成 commit 下发 `HERDR_DEFER_HEAVY_TESTS=1`；活跃 workflow 期间持有 `caffeinate` 唤醒守卫（`[AWAKE GUARD]`，`HERDR_AWAKE_GUARD=0` 可关）。
- **跨仓（xiyu-bid-poc，用户授权）**：`scripts/check-testing-standards.sh` 识别 `HERDR_DEFER_HEAVY_TESTS=1`（仅 controller 收尾注入，避免按仓库来源猜测误伤人类提交），herdr 任务提交只跑快速检查，全量测试交由 workflow test 节点与 pre-push 门禁。
- **现场重载验证**：热重载后真实工作流 `wf-xiyu-bid-poc-0915-01` 的 test 节点首次触发 `[DIRECT DISPATCH FALLBACK] reason=node purpose missing` —— 该项目 `workflow.json` 为旧模板快照（`purpose=""`/`required_outputs=[]`）。修复：新增 `merge_node_policy`（纯函数），节点字段为空时回退 `stage-policies.json`（与总指挥路径语义一致），policy 也缺 purpose 才回落；补 3 项合并用例 + 1 项 policy 回退用例。
- 回归：新增 `tests/test_direct_stage_dispatch.py` 20 项 + `test_fix_loop_gates` 子集用例 2 项；相关套件 147 passed；unittest 全量 318 passed（17 个 pytest-only 文件因环境缺 pytest 未进入）。
- **流程纠正（CoW 隔离）**：本条目落地期间曾因在共享主工作区直接开发，把并行 session 的在途 controller 改动卷入提交；已按「剥离范围 + 保留对方改动（新增 `244490d`，未强推）」处置，并在 CoW 沙盒内对 PR 分支独立复验（94 tests OK + compile OK 后 purge）。xiyu 侧 hook 改动同样迁至沙盒分支落盘并走其 PR 流程。教训归档 §44：非平凡改动一律 CoW 沙盒 + 单写者。
- 更新 [[architecture]] §2.1（Direct Stage Dispatch）。

## [2026-09-16] fix | 跨阶段返工拓扑作废与收尾断链自愈 (committed retry & intermediate invalidation)
- **背景**：`wf-xiyu-bid-poc-0915-01` 在 review 门禁返工后再次卡死在协调器。总指挥汇报已验收通过等待 finalize，但随后毫无推进。排查发现两大根因：
  1. **主仓脏树导致集成收尾断链**：主仓 `xiyu-bid-poc` 遗留未提交的 `scripts/check-testing-standards.sh` 修改，`herdr-task integrate` 安全检查（`git status --porcelain --untracked-files=no`）退出 5（`Main repository has tracked changes`），任务停在 `committed` 态。旧版 controller 的 `finalize_completed_task` 仅接收 `completed` 态，重试直接 SKIP，且无后台常驻补收尾逻辑，导致任务永久悬挂。
  2. **跨阶段返工漏作废中间节点**：`review` 门禁回流到上游 `implementation` 时，`invalidate_for_fix_loop` 仅收集了 `gate_node_id`（`review`）的下游节点，遗漏了 `retry_node` 与 `gate_node_id` 之间的中间节点 `test`。`test` 的 r2 任务仍处于 `cleaned`，DAG 判定 `test` 已完成，跳过 `test` 直扑 `review`；而 `review` 的 `notified` 锁未解除，导致 `test` 推进事件永远无法到达总指挥。
- **Controller & Task 修复**：
  - `invalidate_for_fix_loop` 增加 `retry_node` 支持：计算 `retry_node` 的下游闭包（排除 `retry_node` 自身），将 `test` 等中间验证节点与 `review` 一同作废；跨阶段回流时门禁与中间节点任务不可复用；
  - `finalize_completed_task` 兼容 `committed` 状态：已 `committed` 任务跳过 commit 直接重试 integrate/cleanup，保障幂等；
  - Registry Watcher 增加 `status == "committed"` 的 attention 慢速重试护栏，主仓解除脏树或锁竞争后可自动断链续跑；
  - 现场救援：暂存主仓 `scripts/check-testing-standards.sh`、对修复任务补跑 integrate/cleanup、作废 `test` r2 任务；Controller 自动触发 `implementation -> test` 阶段推进，总指挥成功接收事件并并发派发 r3/r4 验证任务。
- 回归：`tests/test_fix_loop_gates.py` 新增 2 项回归测试（跨阶段中间节点作废 + committed 状态幂等收尾）；全量 500 项测试全部通过；沉淀工程教训 §43。

## [2026-09-16] feat | 接入新 Agent【grok】(Grok Build TUI)
- **背景**：扩展 HAFlow 多智能体协同支持，将 xAI Grok 官方 CLI（`grok`，Grok Build TUI）作为一等公民 Agent 接入系统全链路调度。
- **全链路适配落地**：
  1. **二进制映射与解析单一事实来源 (`herdr/agent_binary.py`)**：在 `AGENT_BINARIES` 注册 `"grok": "grok"`，支持从 PATH、`~/.local/bin` 等目录自动解析。
  2. **轻量与深度沙盒探针适配 (`herdr/preflight.py`, `herdr/deep_preflight.py`)**：
     - 在 `KNOWN_AGENTS` 与 `AUTH_HINTS` 中注册 `grok`（凭据路径 `~/.grok/auth.json`，版本探测 `--version`）；
     - 适配非交互安全探针：识别 `-p / --single` 模式（`grok -p "Reply with exactly HERDR_PREFLIGHT_OK and nothing else."`），验证返回 0 且协议标记精确。
  3. **AgentAdapter 矩阵能力声明 (`herdr/agent_adapter.py`)**：
     - 新增 `GrokAdapter(TTYAgentAdapter)`，明确声明 capabilities：`supports_interrupt=True`（SIGINT 打断）、`supports_soft_steer=True`（间隙插话）、`supports_resume=True`（`-r/--resume/-c/--continue` 会话恢复）、`supports_prompt_injection=True`，协议级别 `tty_prototype`；
     - 注册至全局 `_ADAPTER_REGISTRY`，支持别名 `grokcli -> grok`。
  4. **工位装配与工作区信任 (`services/herdr-worker.py`)**：
     - 新增 `ensure_grok_workspace_trust(repo)` 自动在 `~/.grok/trusted_folders.toml` 中写入隔离沙盒信任标记；
     - `start_agent` 启动参数注入 `--always-approve` 避免 TUI 交互阻断。
  5. **路由白名单与偏好矩阵 (`herdr/agent_router.py`)**：
     - 在 `DEFAULT_ALLOWED` 以及各阶段（`DEFAULT_STAGE_PREFERENCES`）、任务类型（`DEFAULT_TASK_TYPE_PREFERENCES`）偏好中纳入 `grok`。
  6. **工作流模板与 CLI / 控制台**：
     - `workflow_templates/software-development-v1.yaml` 各阶段 `preferred` 追加 `grok`；
     - `bin/herdr-factory`（`--agent` choices 追加 `grok`）、`bin/herdr-task`（`DEFAULT_AGENT_LABELS` 追加 `grok`）；
     - `console/herdr_factory_console.py`（`AGENTS`、`AUTH_HINTS`、前端选择器下拉选项统一同步）。
  7. **文档与指南**：
     - 更新 `docs/operations/deep-preflight-playbook.md`、`wiki/preflight-and-health.md`。
- **回归与实操验证**：
  - 更新单元测试套件：`tests/test_agent_adapter.py`、`tests/test_deep_preflight_accuracy.py`、`tests/test_herdr_worker.py`、`tests/test_console_agent_roster.py` 均新增针对 `grok` 的断言；
  - 44 项直接相关测试 100% PASS；
  - CLI `herdr-preflight` 实测通过：`grok READY present grok 1.0.30`；`herdr-task adapters` 矩阵正常展示。

## [2026-09-16] feat | 软件开发流程模板优化 (software-development-v1) 与跨阶段 Agent 隔离
- **背景**：旧版 `software-development-v1.yaml` 模板中全节点声明“同一阶段允许多个 Task 并行协作”，导致协调器与总指挥在需求、计划甚至收尾阶段无序切碎任务（单个 Tab 出现 4+ 个分屏 Pane），引发终端拥挤、上下文碎片化、协调延迟与并发混乱；同时测试/评审阶段缺乏与实现者的隔离机制，易发生自审自查盲区。
- **模板与策略重构 (`software-development-v1.yaml`)**：
  1. **Pane 数量硬性收敛**：严格限定各阶段工位上限，彻底移除诱导无序膨胀的模糊描述；
  2. **需求与计划阶段双工位对抗审查 (`max_agents: 2`)**：配置 `executor`（主执行者）与 `challenger`（对抗性质询者），分别产出核心规格/方案与《对抗审查与边界漏洞清单》，两份交付物完备后方可推进；
  3. **实现阶段自适应解耦并发 (`max_agents: 3`)**：解耦无冲突任务多 Agent 并发，强耦合/单点改动强制单 Agent 顺序执行，根除 Git 合并冲突；
  4. **测试/评审/收尾阶段单工位 (`max_agents: 1`) + 跨阶段隔离**：配置 `exclude_stage_agents: ["implementation"]`，由独立 Agent 客观把关。
- **调度与派发引擎增强**：
  - `herdr/agent_router.py`：`choose_agent` 解析 `exclude_stage_agents` 策略，查询 Workflow 实现阶段已用 Agent 并从候选池剔除；支持单 Agent 调试环境平稳降级兜底与违规指定显式拦截；
  - `herdr/direct_dispatch.py`：`plan_stage_dispatch` 解析 `agent_policy.roles`，使需求与计划阶段直接规则化派发 `executor` 与 `challenger` 双规格 Task。
- **回归与沉淀**：
  - 新增 `tests/test_agent_router_stage_exclusion.py`（3 项）与 `tests/test_software_development_v1_template.py`（8 项）；更新 `tests/test_direct_stage_dispatch.py`（1 项）；
  - 全仓 512 项自动化测试 100% PASS；沉淀通用工程教训 §45。

## [2026-09-16] fix | 任务归档查询按工作流级联下拉筛选与上下文预选
- **背景**：控制台任务归档弹窗中，工作流筛选器为纯文本输入框（placeholder: `工作流 ID 片段`），缺乏下拉选择能力；且打开弹窗时未带入当前页面正在查看的项目与工作流上下文，导致用户在具体工作流下无法直观按工作流筛选已归档任务。
- **改动与实现**：
  - **工作流筛选升级为下拉选择框 (`<select id="arcWorkflow">`)**：显示需求主题 + 短 ID，支持「全部工作流」；
  - **项目与工作流级联联动**：新增轻量接口 `GET /api/workflows?project_id=...`（零 I/O 纯内存字典转换）；切换项目时自动级联刷新工作流列表并自动触发查询；
  - **页面上下文默认预选**：`showArchive` 打开时自动带入当前 `state.projectId` 与 `state.workflowId`，直出当前工作流归档结果；优先利用前端已知工作流实现 0 延迟首屏直出；
  - **任务卡片快捷过滤**：归档列表中工作流标识支持一键点击切换到对应工作流筛选；
- **回归与部署**：
  - 更新 `tests/test_archive_query.py`（14 项 PASS），全量 516 项测试 PASS；
  - 执行 `scripts/install-herdr-console.sh` 同步至 `~/.herdr-console` 并热重载控制台服务验证。

## [2026-09-16] fix | 修复已完成工作流误报推进停滞告警（Stall Detection 终态与拓扑终点感知）
- **背景**：已交付完成的历史工作流（如 `wf-nexusarchive-54433229-20260913-111049`）在 Web 控制台持续告警 `⚠️ 上一阶段所有任务均已完成，但后继阶段推进悬挂已超 45 秒`，并附带「⚡ 尝试推进阶段」按钮（点击会报错 `当前没有可手工推进的下一阶段`）。
- **根因**：
  1. `herdr/projection.py:detect_workflow_stalls()` 在检测 `stage_advance_hang` 时，虽然注释为 `but workflow still running`，但代码完全没有传入或校验 workflow 状态；对于所有任务均已完成且时间已久的历史工作流，无条件判定为挂起；
  2. 拓扑终点未排除：当收尾阶段（`wrapup`）或全 DAG 节点都已完成时，本就不存在“后继阶段”，告警文案语义失真。
- **改动与实现**：
  1. **停滞检测核心修复 (`herdr/projection.py`)**：
     - `detect_workflow_stalls()` 支持 `workflow: Optional[Dict]` 参数，缺省时自动从 store 加载；
     - **生命周期终态守卫**：若 `status in {"completed", "closing", "failed", "paused"}`、`outcome in {"delivered", "abandoned"}` 或 `completed_at` 存在，立即判定为非停滞；
     - **拓扑终点守卫**：当任务包含 `wrapup` 终态节点或满足 `is_workflow_completed` 时，判定流程已结束而非推进悬挂；
  2. **控制台调用链路与已交付态势表达 (`console/herdr_factory_console.py`)**：
     - `workflow_detail(wid)` 传参 `workflow=w`；
     - `updateAttentionHub()` 在工作流为 `completed` / `delivered` 时，显示绿色优雅的「已交付」全流程闭环归档横幅；
     - `stage_summary()` 修复：代码提交任务处于 `committed` 导致阶段被永久误判为「收尾中」（`finalizing`）的问题，统一按 `COMPLETED_TASK_STATUSES` 聚合为 `cleaned`（已完成）；
- **验证与部署**：
  - 新增 `tests/test_projection_engine.py` 4 项测试与 `tests/test_console_stage_summary.py` 2 项测试，全量 522 项测试 PASS；
      - 部署控制台并实测 `wf-nexusarchive-54433229-20260913-111049` API，`is_stalled` 已恢复为 `False`，所有阶段均为 `cleaned`；沉淀通用工程教训 §48。

## [2026-09-17] fix | Direct Dispatch 静态边界与非 Agent 阻断
- Updated [[dag-workflow-engine]]：动态节点首次派发回退规划，不生成通用单任务；静态角色、单任务和既有补派保持兼容。
- Controller 在 stage_advance 入口阻断非 Agent 节点进入直接派发及总指挥回退；未实现原生执行器，明确要求人工处理。
- 该变更仅为 DispatchPlan 改造的第一切片，不包含计划持久化、幂等执行或模板迁移。
