# HAFlow 真实复杂性来源与反模式审计报告 (Complexity Findings)

> 审计基线：Git Commit `739ef0d`  
> 审计原则：拒绝“只凭代码多就判定缺陷”，一切结论必须附带真实代码路径、触发条件、实测证据与最小修复建议。  
> 审计日期：2026-10-29

---

## 缺陷与反模式概览矩阵

| 编号 | 反模式分类 | 问题简述 | 严重级别 | 复现状态 | 核心涉及模块 |
|---|---|---|---|---|---|
| **F-01** | B. 多重门禁 / 死胡同 | Staged 流程自动验收失败后事件被旁路丢弃，任务永久死锁在 `agent_done` | **P0 (阻塞)** | **已复现** | `services/controller/task_events.py` |
| **F-02** | E. 非确定性流程控制 | 计划阶段 `executor` 与 `challenger` 并发派发导致时序倒挂与引用缺失死锁 | **P0 (阻塞)** | **已复现** | `herdr/direct_dispatch.py`, `herdr/plan_parser.py` |
| **F-03** | D. 环境与系统调用缺陷 | `stage_skills.py` 与 `git_coordination.py` 在受限环境下抛出未捕获 `PermissionError` 导致调度崩溃 | **P0 (阻塞)** | **已复现** | `herdr/stage_skills.py`, `herdr/git_coordination.py` |
| **F-04** | E. 非确定性流程控制 | 依赖硬编码中文正则做需求与计划交付物合法性裁决，英文或不同表述被误杀 | **P1 (高危)** | **已复现** | `herdr/stage_skills.py` |
| **F-05** | A. 决策权分散 | 阶段推进资格在 4 个模块、8 个层级中分散裁决，责任主体模糊 | **P1 (高危)** | 代码确认 | `advancement.py`, `stage_delivery.py`, `direct_dispatch.py` |
| **F-06** | F. 过度检查 | `_scheduler_join_gate_allows()` 在单次推进事件中被无缓存连续重复计算 3 次 | **P2 (中危)** | 代码确认 | `advancement.py`, `stage_delivery.py`, `direct_dispatch.py` |
| **F-07** | D. 状态语义冲突 | 多事实源（SQLite `state.db` vs 内存文件 `stage-state.json` / `tasks.json`）引发裂脑与锁残留 | **P1 (高危)** | 历史复盘确认 | `stage_state.py`, `state_store.py` |

---

## 详细缺陷剖析与代码证据

### F-01：Staged 流程自动验收失败后事件被旁路丢弃，任务永久死锁在 `agent_done` (Anti-Pattern B & C)

1. **实际代码路径**：
   [`services/controller/task_events.py:48-64`](file:///Users/user/haflow/services/controller/task_events.py#L48-L64)
   ```python
   # 规则化验收快路径:非门禁节点铁证齐备直接 completed,
   # 门禁节点有唯一结论标记则直接落 verdict;两者都不命中才回落总指挥。
   if event_type == "done" and (
       ports.try_auto_accept(task_id) or ports.try_auto_verdict(task_id)
   ):
       ports.attention_clear(key)
       ports.finalize_completed_task(task_id)
       return

   _task_pre = ports.get_task(task_id)
   if _task_pre:
       _wf_id = _task_pre.get("workflow_id")
       _wf_cfg = ports.workflow_config_for(_wf_id) or {} if _wf_id else {}
       from herdr.stage_skills import is_staged_dev_flow_workflow
       if is_staged_dev_flow_workflow(_wf_cfg):
           _r = locals().get("_defer_reason", event_type)
           ports.attention_note(
               key,
               _task_pre,
               event_type,
               reason=f"auto_acceptance_rejected: {_r}" if event_type == "done" else str(_r),
               detail=f"Task {task_id} {event_type} in staged workflow; coordinator bypass active",
           )
           print(f"[STAGED TASK EVENT BYPASS] task={task_id} event={event_type} reason={_r}")
           return
   ```
2. **触发条件**：
   在 `software-development-v1` 中（由 `coordinator_required: false` 判定为 `is_staged`）：
   - `requirements` 任务未输出全部 5 个中文匹配段落；
   - 或 `plan` 任务的 Challenger 审查缺少 `Plan-Ref: <note_id>`；
   - 或 `implementation` 任务的工作区改动未被 `verify-baseline` 检出；
   - 或 `test`/`review` 门禁任务未能在终端或 JSON 中输出标准的 `HERDR_GATE_VERDICT: pass` 标记。
3. **可复现证据**：
   在 PR #356 的测试 [`tests/test_software_development_v1_adapter.py:734`](file:///Users/user/haflow/tests/test_software_development_v1_adapter.py#L734) 中明确固化了这一行为：
   ```python
   # test_29_real_workflow_template_config_recognition_and_task_event_bypass:
   # 验证 auto_acceptance 失败时，写入 attention_note 且直接 return，
   # 绝不调用 ensure_coordinator_running
   ```
   **但该测试没有断言该 Task 之后该如何继续推进**！
   在真实环境中，由于 Controller 在此处直接 `return`，任务状态依然保持为 `agent_done`。在后续每 2 秒的 `registry_watcher` 循环中：
   - `redeliver_done_event()` 重新将 `done` 入队；
   - 入队后再次触发 `_handle_task_event()`；
   - 再次失败并输出 `[STAGED TASK EVENT BYPASS]` 并 `return`；
   - 形成无限空转死循环，工作流永远停在当前节点，**必须人工干预执行 `herdr-task set ... completed` 才能解救**。
4. **对自动执行的影响**：
   这是导致 HAFlow 实际运行中“经常无法自主完成、节点执行结束后无法自动进入下一节点、需要大量人工介入”的**头号核心代码根因**。
5. **是否已有测试覆盖**：
   已有正向测试证明旁路逻辑生效，但缺少故障闭环恢复测试。
6. **最小修复建议**：
   在 Staged 流程中，当自动验收或裁决失败时，**绝不能无声丢弃事件**。应根据失败类型：
   - 属于可修复的代码/文档格式缺陷，自动跃迁至 `rework`，向工位注入具体的拒绝原因（如“缺少段落：验收标准”），触发 Agent 补全；
   - 属于严重阻断或耗尽重试时，状态跃迁至 `blocked` 或 `failed`，由 Controller 明确报告阻断，停止无意义的空转。

---

### F-02：计划阶段 `executor` 与 `challenger` 并发派发导致时序倒挂与引用缺失死锁 (Anti-Pattern E & C)

1. **实际代码路径**：
   - 并发派发：[`herdr/direct_dispatch.py:898-933`](file:///Users/user/haflow/herdr/direct_dispatch.py#L898-L933)
   - 时序与引用核验：[`herdr/plan_parser.py:549-570`](file:///Users/user/haflow/herdr/plan_parser.py#L549-L570)
2. **触发条件**：
   `requirements` 节点完成后，Controller 推进至 `plan` 节点。
   `plan` 节点配置中声明了两个角色：`roles: [executor, challenger]`。
   `plan_stage_dispatch` 遍历 `roles`，**一次性生成并同时启动了两个任务**：
   - `wf-plan-executor`（负责编写架构与拆分方案）
   - `wf-plan-challenger`（负责对抗质询与可行性审查）
3. **可复现证据**：
   在真实运行时，Challenger 与 Executor 同时启动：
   - 此时 Executor 尚未写完方案，Challenger 在其工作区或 shared notes 中找不到有效方案；
   - 若 Challenger 稍早结束（`chal_ts < plan_ts`），或者 Challenger 未能得知 Executor 最终落盘的 `note_id`，Challenger 输出的审查报告将缺少 `Plan-Ref: <note_id>`；
   - 当两者均处于 `completed`，后续推进 `implementation` 时，`plan_stage_dispatch` 调用 [`herdr/plan_parser.py:verify_plan_and_challenger()`](file:///Users/user/haflow/herdr/plan_parser.py#L447)：
     ```python
     if plan_ts > chal_ts > 0.0:
         return False, f"challenger_outdated: plan was updated after challenger review...", ...
     if not target_ref:
         return False, "challenger_plan_ref_missing: challenger approved without specifying Plan-Ref...", ...
     ```
   - 触发上述任一拒绝，`direct_dispatch` 返回 `mode: "refusal"`，Controller 记录 Attention 后终止推进！
4. **对自动执行的影响**：
   这是一个**先有鸡还是先有蛋的拓扑结构矛盾**：业务语义上 Challenger 是对 Executor 成果的审查（存在严格因果先后顺序），但模板与派发引擎却将两者作为同一节点的无依赖并发角色同时拉起，并在后续消费时施加严格的时序因果约束，导致天然产生并发时序竞争。
5. **是否已有测试覆盖**：
   测试套件中使用预设好的 mock 交付物（人工对齐了时间戳与 `Plan-Ref`）掩盖了这一现场并发问题。
6. **最小修复建议**：
   - **方案 A（最小拓扑调整）**：在 `plan` 节点内部建立因果时序门禁：`plan` 节点就绪时仅派发 `executor`；当 `executor` 完成并产出有效计划 `note_id` 后，再派发 `challenger`，并在 prompt 中直接注入该 `plan_id`；
   - **方案 B（规则引擎放宽）**：若在同一节点内单次运行，允许 Challenger 继承当前节点的 Plan，时间戳比较放宽为生命周期容差范围。

---

### F-03：`stage_skills.py` 与 `git_coordination.py` 在受限环境下抛出未捕获 `PermissionError` 导致调度崩溃 (Anti-Pattern D)

1. **实际代码路径**：
   - 路径 1：[`herdr/stage_skills.py:99-101`](file:///Users/user/haflow/herdr/stage_skills.py#L99-L101)
     ```python
     homes = {Path.home(), _get_real_home()}
     for h in homes:
         for p in (h / ".claude" / "skills", h / ".gemini" / "config" / "skills", ...):
             if p.exists() and p not in roots:  # 未捕获 PermissionError!
                 roots.append(p)
     ```
   - 路径 2：[`herdr/git_coordination.py:81-87`](file:///Users/user/haflow/herdr/git_coordination.py#L81-L87)
     ```python
     def _default_process_provider():
         result = subprocess.run(
             ["ps", "-axo", "pid=,command="], # 未捕获 PermissionError!
             text=True, capture_output=True, check=False,
         )
     ```
2. **触发条件**：
   - 路径 1：macOS 或沙盒环境中，用户的部分应用目录（如 `~/.gemini`）具有受限访问权限或隔离属性。调用 `p.exists()` 内部执行 `os.stat`，抛出 `PermissionError: [Errno 1] Operation not permitted`。
   - 路径 2：沙盒或无进程枚举权限的环境中，调用 `subprocess.run(["ps", ...])` 在 `fork_exec` 阶段抛出 `PermissionError: [Errno 1] Operation not permitted: 'ps'`。
3. **可复现证据 (现场实测)**：
   运行本地现有测试集直接复现：
   ```bash
   pytest -q tests/test_software_development_v1_adapter.py -k test_02
   # 报错：PermissionError: [Errno 1] Operation not permitted: '/Users/user/.gemini/config/skills'
   
   pytest -q tests/test_node_dispatch_contract.py
   # 报错：PermissionError: [Errno 1] Operation not permitted: 'ps'
   ```
4. **对自动执行的影响**：
   只要涉及装配技能上下文（`assemble_stage_context`）或派发检查（`ensure_no_git_processes`），系统调用就会产生未处理崩溃，导致整个派发流程直接中断。
5. **是否已有测试覆盖**：
   现有测试未 mock 这两处系统调用，在普通未受限环境正常，在真实权限收紧环境中必然失败。
6. **最小修复建议**：
   - `stage_skills.py`：对 `p.exists()` 增加 `try ... except (OSError, PermissionError): continue`；
   - `git_coordination.py`：对 `subprocess.run(["ps", ...])` 增加 `try ... except (OSError, subprocess.SubprocessError): return []`。

---

### F-04：依赖硬编码中文正则做需求与计划交付物合法性裁决 (Anti-Pattern E & F)

1. **实际代码路径**：
   [`herdr/stage_skills.py:421-446`](file:///Users/user/haflow/herdr/stage_skills.py#L421-L446)
   ```python
   _REQ_GOAL_RE = re.compile(r"(用户目标|需求目标|需求背景|问题与假设|目标与范围)", re.IGNORECASE)
   _REQ_SCOPE_RE = re.compile(r"(功能范围|需求范围|非目标|业务规则|边界)", re.IGNORECASE)
   _REQ_AC_RE = re.compile(r"(验收标准|验收条目|AC-\d+|验收方案)", re.IGNORECASE)
   _REQ_PROOF_RE = re.compile(r"(证明方式|验证计划|proof_mode|验证命令)", re.IGNORECASE)
   _REQ_CEILING_RE = re.compile(r"(交付终点|交付上限|delivery_ceiling)", re.IGNORECASE)
   ```
2. **触发条件**：
   Agent 在 `requirements` 节点根据提示词自主工作，产出了英文规格书（例如标准 Markdown 章节：`# Requirements Document`、`## Goals and Objectives`、`## Scope`、`## Acceptance Criteria`、`## Verification Plan`）。
3. **可复现证据**：
   将英文合规文本输入 `validate_requirements_deliverable_content()`，返回值必为 `(False, ['缺少用户目标与需求背景', '缺少功能范围或非目标说明', ...])`。
   进而触发 F-01 的 Staged Bypass，导致任务卡死在 `agent_done`。
4. **对自动执行的影响**：
   严重降低了 Agent 自主完成率，将大语言模型的自由文字表述强行绑定到几个特定中文词汇上，只要 Agent 措辞略有不同就会造成流程整体阻断。
5. **是否已有测试覆盖**：
   仅覆盖了中文标准样例测试，无英文与同义词鲁棒性测试。
6. **最小修复建议**：
   扩展正则表达式词库，支持标准英文技术词汇（`Goal`, `Scope`, `Acceptance`, `Verification`, `Ceiling`）及常见同义词；或引入轻量 JSON Frontmatter / 结构化模板规范。

---

### F-05：阶段推进资格在 4 个模块、8 个层级中分散裁决，缺少明确主控责任 (Anti-Pattern A)

1. **实际代码路径**：
   - 扫描层：[`services/controller/registry.py`](file:///Users/user/haflow/services/controller/registry.py)
   - 评估层：[`services/controller/advancement.py`](file:///Users/user/haflow/services/controller/advancement.py)
   - 准入层：[`services/controller/stage_delivery.py`](file:///Users/user/haflow/services/controller/stage_delivery.py)
   - 派发层：[`services/controller/direct_dispatch.py`](file:///Users/user/haflow/services/controller/direct_dispatch.py)
   - 规划层：[`herdr/direct_dispatch.py`](file:///Users/user/haflow/herdr/direct_dispatch.py)
   - 解析层：[`herdr/plan_parser.py`](file:///Users/user/haflow/herdr/plan_parser.py)
   - 终化层：[`services/controller/git_finalize.py`](file:///Users/user/haflow/services/controller/git_finalize.py)
   - 门禁层：[`services/controller/auto_verdict.py`](file:///Users/user/haflow/services/controller/auto_verdict.py)
2. **触发条件**：
   任意一个节点从完成到进入下一节点，需要在上述 8 个不同层级中依次做出“放行”判定。
3. **对自动执行的影响**：
   任何一个层级的轻微不一致（例如 `advancement.py` 认为就绪并入了队，但 `stage_delivery.py` 认为准入不合格；或者 `direct_dispatch.py` 认为规划不满足返回 `refusal`），都会在不同模块产生不同的副反应（有的清锁，有的不清锁，有的记 Attention，有的默默丢弃）。排查一个卡住的流程需要通读 8 个文件上千行代码，排障成本极高。

---

### F-06：`_scheduler_join_gate_allows()` 连续重复计算 3 次 (Anti-Pattern F)

1. **实际代码路径**：
   - 第 1 次：[`services/controller/advancement.py:323`](file:///Users/user/haflow/services/controller/advancement.py#L323)
   - 第 2 次：[`services/controller/stage_delivery.py:64`](file:///Users/user/haflow/services/controller/stage_delivery.py#L64)
   - 第 3 次：[`services/controller/direct_dispatch.py:81`](file:///Users/user/haflow/services/controller/direct_dispatch.py#L81)
2. **触发条件**：
   任何节点进入推进与派发流程。
3. **对自动执行的影响**：
   每次计算均需要通过 `ports.load_tasks()` 重新遍历所有任务并解析依赖分支的 Candidate SHA。由于 3 次计算处于不同线程与时刻，存在脏读风险，且徒增无意义的 CPU 与磁盘 I/O 开销。
4. **最小修复建议**：
   遵循 Functional Core 架构，在入队前统一由 `advancement.py` 判定一次并将判定结果附带在事件 payload 中，后续执行外壳仅读取验证结果，不再重复求值。

---

### F-07：多事实源（SQLite vs 文件 JSON）引发裂脑与锁残留 (Anti-Pattern D)

1. **实际代码路径**：
   [`services/controller/stage_state.py:200-240`](file:///Users/user/haflow/services/controller/stage_state.py#L200-L240): `reconcile_stage_advance_states()`
2. **触发条件**：
   Controller 进程在派发过程中发生崩溃、热重启（`kickstart`）或断电。
3. **事实与代码证据**：
   代码注释明确记录了真实工程事故：
   > *"Real deadlock: wf-nexusarchive-1005-01, where `test`/`review` kept their 'notified' lock forever and the controller skipped both every sweep with no log line at all."*
   `stage-state.json` 是一个无事务保障的文件锁，而 `node_dispatch_operations` 属于 SQLite。一旦两边状态脱节，节点将被永远判定为“已通知总指挥”，排斥在一切调度之外。
4. **最小修复建议**：
   彻底淘汰 `stage-state.json`，将阶段状态与排队锁统一收敛到 SQLite 的单事务中管理。
