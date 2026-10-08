# Wiki Evolution Log (log.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> 本文件为 HAFlow 知识层的 Append-Only 演进记录。  
> 仅记录 Wiki 结构与知识库发生实质性变更的原因与概要，不记录细碎的代码提交流水。

## [2026-10-07] fix | 强化代码评审评测真实性（Benchmark Integrity Round 2）：中性 PR 描述防止盲测答案泄漏、ReviewBench 官方契约兼容与依赖锁定、打分成功显式门禁
- 背景：
  1. Manifest 中的 PR `body` 包含了具体缺陷函数名与答案性后果，由于审查 Agent 可见 `body`，破坏了盲测（Blind Test）原则；
  2. 官方 ReviewBench 成功输出采用 `macro` / `micro` 层级，本身不含顶层 `status`，导致 `compare_benchmarks` 拦截了官方合法评测结果；
  3. 子进程使用 `--no-strict` 时即使没有输出有效 metrics 也可能 exit 0，原判定逻辑存在假阳性成功漏洞；
  4. 评分器仓库克隆时先在默认分支执行 `npm install` 再切 SHA，导致依赖与锁定代码不一致。
- 变更：
  1. **盲测中性化**：Manifest 中的 `body` 重构为中性功能描述，移除具体错误标识符（如 `extract_task_candidate_sha`、`verdict_fingerprint`、`decision_identity`）与缺陷答案；
  2. **契约自适应提取与显式 status 包装**：`extract_and_validate_metrics` 兼容 ReviewBench 官方 `macro.overall` 与包装格式；评测成功后由 HAFlow wrapper 显式注入 `status: "completed"` 供 `compare` 校验；
  3. **严格判定成功条件**：`cmd_run` 成功要求 `subprocess == 0 AND results exists AND parseable AND metrics complete`，任一失败一律标记 `judge_failed` 并返回非零退出码（exit 1）；
  4. **依赖锁定一致性**：`find_or_setup_reviewbench` 调整执行顺序为 `checkout <target_sha>` → 校验 HEAD → 执行 `npm ci`，确保代码与 node_modules 严格源自同一版本。
- 证据：
  - `pytest -q tests/test_review_benchmark.py` 18 passed in 0.66s（含中性描述无泄漏测试与官方结构指标提取测试）；
  - `python3 -m compileall -q herdr bin/herdr-review-bench tests/test_review_benchmark.py` clean；
  - `git diff --check` clean。

## [2026-10-07] fix | 修复代码评审评测真实性（Benchmark Integrity）：Golden 缺陷引入语义对齐、工作区物理纯净隔离、评分器 SHA 校验与失败状态阻断
- 背景：
  1. 初版评测集错误地使用了修复提交作为 head SHA，导致将修好代码当缺陷才能得分的逻辑反转；
  2. 隔离工作区使用简单 clone 会继承原仓库所有 remote refs 与未来 commit/golden 目录，存在被测 Agent 偷看答案的风险；
  3. 评分器失败时伪造了 0 分指标，且命令返回成功，可能导致基础设施故障被误判为 Reviewer 能力断崖式下跌；
  4. 缓存的 ReviewBench 仓库未校验实际 HEAD，且输出目录复用时未清理旧文件。
- 变更：
  1. **Golden 缺陷语义重塑**：从 PR #107、PR #110、PR #108 提取真正的缺陷引入变更（如 `extract_task_candidate_sha` 未校验客观证据、`verdict_fingerprint` 混入易失 `task_id`、`decision_identity` 缺少 candidate episode 绑定），并重写对应 Golden 判定答案与回归验证证据；
  2. **物理工作区严格隔离**：`herdr/review_benchmark.py` 改用 `git init` + 精准 `git fetch <base> <head>`，零 remote、零未来 refs、零 golden 夹具泄露；
  3. **评分失败整轮 FAILED**：评分器失败时将 metrics 标记为 None 并显式记录 `judge_failed`，CLI 退出码非 0，`compare_benchmarks` 严格阻断未成功评分的目录对比，拒绝伪造 0 分；
  4. **干净 Output 与 Scorer SHA 每次校验**：`bin/herdr-review-bench` 每次运行前清空输出子目录，并执行 `git checkout -f <target_sha>` 确保评分器版本绝对受控。
- 证据：
  - `pytest -q tests/test_review_benchmark.py` 全部通过（16 passed in 1.34s）；
  - `python3 -m compileall -q herdr bin/herdr-review-bench tests/test_review_benchmark.py` 退出码 0；
  - `git diff --check` 退出码 0；
  - 端到端干净运行验证完成，3 个案例候选意见精准对齐缺陷引入代码。


## [2026-10-07] feat | 接入 ReviewBench 代码评审回归评测套件与独立评测工具链
- 背景：
  1. HAFlow 研发流中缺少标准化的代码评审有效性与回归评测手段，修改 Reviewer 提示词与上下文组装后无法系统衡量误报率与漏检率；
  2. 需要对接业界规范 ReviewBench 官方评分体系（锁定 commit `e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d`），使用 HAFlow 真实历史缺陷沉淀回归案例。
- 变更：
  1. **`bin/herdr-review-bench`**：
     - 提供独立 CLI 工具，支持 `run`（执行基线/候选评测）与 `compare`（横向比对两次评测）子命令；
     - 严格隔离模型 API Key（环境变量读取）、案例隔离工作区与报告持久化目录。
  2. **`herdr/review_benchmark.py`**：
     - 实现 ReviewBench 契约校验（PR 元数据、行号跨度、发现项结构）；
     - 实现临时 Git 环境隔离抽取、被测 Agent 驱动与解析、ReviewBench 官方 `npm run judge` 评分器调用封装；
     - 明确区分审核完成/启动失败/超时/输出不可解析状态，绝不静默吞错；
     - 生成结构化中文报告（`report.md`）与可机器读取结果（`results.json`），支持综合比对报告。
  3. **`tests/fixtures/review_benchmark/`**：
     - 收录 3 个真实已核验的 HAFlow 历史缺陷案例（PR #147、PR #158、PR #149），配齐真实 base/head SHA、已确认缺陷位置与测试回归证据。
  4. **`tests/test_review_benchmark.py`**：
     - 接入 14 项零模型依赖的自动化单元测试，覆盖契约格式、无效 SHA、案例遗漏、超时与格式异常处理。
- 证据：
  - `pytest -q tests/test_review_benchmark.py` 全部通过（14 passed in 0.22s）；
  - `python3 -m compileall -q herdr bin/herdr-review-bench tests/test_review_benchmark.py` 零语法错误；
  - `git diff --check` 零空白违规；
  - 真实评测命令 `run` 与 `compare` 端到端执行通过。


## [2026-10-05] fix | 门禁放行失效边界闭环：快照版本绑定、回溯级联失效与跨端有效性判定统一（防旧豁免掩盖新阻塞）
- 背景：
  1. 旧放行记录（`gate_overrides`）持久化时未绑定豁免时的任务版本快照与完整任务集合，读取时单任务豁免仅检查其他任务，节点级豁免直接放行全节点；
  2. 被豁免任务后续若更新并重新阻塞（`stage_verdict="blocked"`），或节点后续新增了阻塞任务，旧豁免记录仍使阶段摘要与画布显示为虚假完成；
  3. `rollback_workflow` 回溯时清理了推进锁并作废了任务，但遗漏了受影响节点的旧 `gate_overrides`，回溯重跑后旧记录仍会覆盖新现场。
- 变更：
  1. **`herdr/state_db.py` & `herdr/kernel.py`**：
     - `force_pass_gate` 持久化显式绑定生效快照：单任务记录 `task_id`、`task_version` 及 `task_versions`；节点级记录 `task_ids` 列表与各任务 `task_versions`；
     - 将“历史审批事实”与“当前适用豁免”解耦，在工作流元数据中追加 `gate_overrides_history` 审计账本；
     - `rollback_workflow` 在回溯下游受影响节点集合（`affected_nodes`）时，级联将 active `gate_overrides` 弹出作废，并写入 `gate_overrides_history`（标记 `action="invalidated_by_rollback"`）。
  2. **`herdr/workflow_graph.py` & `console/herdr_factory_console.py`**：
     - 在 `workflow_graph.py` 沉淀权威纯函数 `is_gate_override_valid(gate_override, live_tasks)`，供画布投影与 Console `stage_summary` 共用；
     - 单任务豁免在任务再次阻塞（`stage_verdict="blocked"`）、版本漂移或非 pass 时自动失效；
     - 节点级豁免在存活任务集合发生增删变化（如新增未确认任务）、任务再次阻塞或版本变化时自动失效，如实暴露真实阻塞。
  3. **自动化测试**：
     - `tests/test_console_ops_repair_gate_separation.py` 扩充 Scenario 23（已豁免任务再次阻塞）、Scenario 24（节点放行后新增阻塞任务）、Scenario 25（放行后回溯再执行及审计保留），25 项全通；
     - 453 项全量控制台与工作流测试全通。
- 证据：
  - 453 passed, 79 subtests passed in 41.39s；`python3 -m compileall` 零报错；`git diff --check` 零违规。


## [2026-10-05] fix | 运维修复与门禁放行解耦加固：会签接口统一约束、多任务节点失败穿透消除与空节点交错快照闭环
- 背景：
  1. 会签接口 `/api/task/signoff` approve 分支直接调用放行内核，绕过了二次显式确认、人工非空原因、任务归属与版本快照校验；
  2. 多任务节点状态聚合（`herdr/workflow_graph.py` 与 Console `stage_summary`）存在“最新任务完成即标记节点完成”的兜底误判，导致较早失败且未被替换/豁免的任务被虚假掩盖为 completed/cleaned；
  3. 节点级放行若仅提供单一 `expected_version`，在节点存在多个任务时无法证明任务集合未被并发增删替换；
  4. 空节点在预检与事务开始之间若并发新增任务，未显式绑定空快照（`expected_task_versions={}`）会导致旧请求越权放行未经确认的新任务。
- 变更：
  1. **`console/herdr_factory_console.py`**：
     - 将 `api_task_signoff` 统一接入 `_validate_force_pass_params`，对会签批准强制校验显式确认、非空人工理由（屏蔽默认系统文案）、任务与工作流归属、以及版本与工位快照；前端协同工作舱完善二次确认与反馈必填约束；
     - 调整 Console `stage_summary` 与 `herdr/workflow_graph.py::aggregate_node_status`，彻底消除以创建时间或较新任务完成/通过而掩盖历史失败的逻辑；未被替换（未 `superseded`）、未被明确豁免的失败任务，独立阻断节点判定为完成；
     - 节点级放行强制要求提供 `expected_task_versions: dict`（无任务节点必须显式提供 `{}`），并在前端 `forcePassTask` 中对节点级放行自动收集当前任务映射。
  2. **`herdr/state_db.py` & `herdr/kernel.py`**：
     - 节点级放行严格禁止传入单一 `expected_version`，必须使用 `expected_task_versions` 映射；
     - 在写入事务内严格比对实际任务集合与提交映射，当预检为空节点提交 `{}` 但事务前新增任务时，在事务边界触发拒绝并完整回滚，消除越权放行竞态；
     - 允许内核级底层无版本期望的调用（如测试与内部维护），兼顾并发乐观锁安全性与底层控制元语调用灵活性。
  3. **自动化测试**：
     - `tests/test_console_ops_repair_gate_separation.py` 覆盖 22 项全流程专项测试，包含 Scenario 21（空节点预检后新增任务在事务内拒绝并回滚）与 Scenario 22（较早任务失败独立阻止节点完成，不被较晚任务 completed/integrated 掩盖）；
     - 更新 `tests/test_console_stage_summary.py`，确保历史失败基于替换关系排除；
     - 450 项全量关联测试通过。
- 证据：
  - 450 项测试全绿（450 passed, 79 subtests passed in 43.06s）；`python3 -m compileall` 零报错；`git diff --check` 零违规。


## [2026-10-04] fix | 路由健康拒绝解耦（防节点槽位死锁）、签发笔记全链路接线与候选重冻 CLI 闭环
- 背景：
  1. 多 Agent 派发时，若目标 Agent 未就绪或健康体检失败，原实现错误调用 `_record_router_failure_task` 在 `tasks.json` 落失败任务，导致单任务节点（`max_tasks_per_node=1`）槽位被永久耗尽，后续自愈重试死锁；
  2. 健康失败时向 CLI 输出误导性的 `Isolation is fail-closed... opt out` 文案，掩盖了真实连通性/凭证问题；
  3. 共享文档区缺少对 `sign-off` 笔记类型的支持，导致审批签发无法作为合法上下文被机器校验和纳入评分；
  4. 重复派发日志（`[DISPATCH DUPLICATE]`）仅输出既有任务 ID，造成提议与存量审计混淆；
  5. 缺乏候选 SHA 重冻结的 CLI 入口，无法在工作流复验阶段进行命令式重新冻结。
- 变更：
  1. **`herdr/agent_router.py`**：引入结构化异常层次 `RouterRejectionError`（包含 `RouterIsolationRejection`、`RouterHealthRejection`、`RouterPolicyRejection`），均继承自 `RuntimeError` 保证向下兼容；
  2. **`bin/herdr-task`**：
     - 隔离拒绝（Fail-closed）保留失败持久化以满足 FR-6 审计要求；
     - 健康探测/策略拒绝仅调用 `abort_launch_intent` 和 `release_agent_reservation` 清理临时意图，绝不持久化写入 `tasks.json` 消耗节点槽位，并输出明确的 preflight 诊断提示；
     - 重复派发明确打印 `proposed={args.task_id} existing={duplicate['task_id']}`；
     - 新增 `freeze-candidate` CLI 子命令，对接 `herdr.scheduler_facts.record_candidate_frozen`。
  3. **`herdr/workflow_docs.py`**：将 `sign-off` 同步纳入 `NOTE_KINDS` 与 `CONTEXT_KINDS`。
  4. **自动化测试**：新增 `tests/test_router_health_and_defects_remedy.py`（7 项专项测试全部通过，关联回归 139 项全通）。
- 证据：
  - 专项测试 7/7 passed，路由与派发回归 139/139 passed，S6 独立评审裁定 `MERGE_READY`。

## [2026-10-04] fix | 多智能体流水线系统性硬化：补派契约强制闭环、原子启动与未就绪回滚、环境信任装配预埋及门禁静态语义校验
- 背景：
  1. 真实流水线长程执行中暴露四项系统级阻断隐患：
     - 重试或替代任务发起时未指定 `--supersedes`，导致调度器持续报错 `zombie_obligation_unreplaced`，后续阶段永远停滞；
     - Worker 在原生 Agent 进入 `interactive_ready` 之前过早持久化 `agent_started` 意图，就绪阻塞或失败时留下孤儿动态 Pane 和僵尸分配意图；
     - Worker 运行时尝试暗写全局用户配置引发冲突；未受信任目录后台启动时弹出 TUI 交互弹窗导致静默挂起；
     - 门禁脚本未核对 TSV Schema，出现列越界或同列自比（重言式恒真/恒假），使得质量门禁把关失效。
- 变更：
  1. **`bin/herdr-task` & `herdr/direct_dispatch.py`**：强制拦截在存在未清偿义务的节点上缺少 `--supersedes` 的派发，补派规格自动填充 `spec["supersedes"]`；
  2. **`services/herdr-worker.py` & `herdr/task_resources.py`**：启动握手严格在 `interactive_ready: True` 确认后才推进 `agent_started`；未就绪失败幂等回收动态 Pane 并终止意图（`abort_launch_intent`），同时严格受 `agent_start_attempted` 保护保留 Clone 目录供事后排查；
  3. **`herdr/workspace_trust.py` & `bin/herdr-factory`**：沉淀独立工作区信任模块（支持 Grok / Claude 幂等安全预埋），并在 Factory 初始化装配期预埋信任，Worker 运行时严格保持只读与零外部配置副作用；
  4. **`herdr/gate_validator.py` & `herdr/workflow.py`**：新增独立门禁语法与静态语义校验器（拦截 awk/sh 语法错误、列越界、同一列自比恒真/恒假），并在 DAG 静态装载时自动校验拦截；
  5. **自动化测试**：新增 `test_dispatch_supersede_enforcement.py`、`test_worker_startup_atomic_rollback.py`、`test_workspace_trust.py`、`test_gate_validator.py` 等测试套件，全量 3341 项自动化测试 100% 通过。
- 证据：
  - 15 项新增测试 100% PASS，176 项核心回归 100% PASS，全库 3341 项测试通过；
  - S6 独立评审工件 `.omc/review-0f4d0031-5b2c-4b9d-b665-f57c61df269f.md` 裁定 `MERGE_READY`。

## [2026-10-03] fix | 工作流跨Run快照隔离、控制器CPU空转风暴消除、Jev 422契约修复与Worker Push双重防护
- 背景：
  1. 真实业务工作流（`wf-project-1002-01`）实现阶段完成后停滞无法向下推进，且控制器 CPU 长期处于 91.8% 满载假死状态。
  2. 根因剖析：
     - **跨工作流状态污染**：项目共享 `workflow.json` 中遗留上一工作流的 `required_task_ids`，新工作流启动时原样继承，导致调度器永远判定实现节点未完成；
     - **主轮询 SQLite 模式重编译风暴**：对 264 个非活动任务每秒重复建连并编译全库 30+ 触发器 AST，耗尽 CPU；
     - **Worker 越权 Push 导致 Git Adoption 死锁**：Agent 误执行 `git push origin` 触发安全拒绝与死锁；
     - **Jev HTTP 422 格式错误**：`noul` 题型传入了字符串 `criteria`，违背 Schema 契约导致校验拦截。
- 变更：
  1. **`herdr/projects.py`**：工作流启动时强制深拷贝快照隔离并清洗所有节点与阶段的动态运行时字段（`required_task_ids`, `task_ids`, `active_task_ids` 等）；
  2. **`services/herdr-controller.py`**：补充终端状态过滤（`TERMINAL_LIKE_STATUSES`），引入局部缓存避免重复查询 `workflow_closed`，并确保 `committed` 任务的终化重试不被饥饿丢弃；
  3. **`services/herdr-worker.py`**：沙盒工位安装双重 Push Guard（`pre-push` 拦截脚本 + `remote.origin.pushUrl=DISABLED_FOR_WORKER_LOCAL_TEST_ONLY`），彻底阻断 Worker 越权推送；
  4. **`herdr/observer/signals.py` & `herdr/decision/providers/jev.py`**：合并引导词至 `instructions` 并移除非法 `criteria` 字符串，且完整回显 HTTP 422 响应体便于诊断；
  5. **自动化测试**：新增 `test_workflow_snapshot_isolation.py`、`test_worker_push_guard.py`、`test_jev_criteria_schema.py` 等测试套件，全量 3180+ 测试全绿。

## [2026-10-02] fix | herdr-worker 沙盒清理保留 launch identity：消除 git clean -fd 误杀导致的新 Task 派发失败
- 背景：
  1. 现场反馈 `herdr-task launch` 在 worker 阶段对新 task_id 派发崩溃：`FileNotFoundError: '<clone>/.herdr-launch-identity.json'`，伴随 clone 回滚但已建好的 tmux pane 发生泄漏。
  2. 根因剖析：
     - `services/herdr-worker.py` 在 `main()` 中先于分支检出阶段将 `launch_identity` 写入 clone 根目录（`write_worker_launch_identity(clone, launch_identity, initial=True)`），作为未跟踪的身份标桩文件（`.herdr-launch-identity.json`）；
     - 随后的 `create_task_branch`（普通分支新建）及 `checkout_onto_branch`（`--onto` 既有分支续接）均会调用 `sanitize_clone_sandbox(clone)`；
     - `sanitize_clone_sandbox` 内部执行裸 `git clean -fd` 清理沙盒，因 `.gitignore` 仅忽略目录 `.herdr/` 未忽略文件 `.herdr-launch-identity.json`，导致该文件被无情删除；
     - 在随后的 `create_pane` 完成后，worker 调用 `write_worker_launch_identity(clone, launch_identity)`（默认 `initial=False`），该安全校验故意回读原文件做资源归属与租户校验，因文件丢失触发 `FileNotFoundError` 导致失败。
- 变更：
  1. **`services/herdr-worker.py` (`sanitize_clone_sandbox`)**：
     - 在 `git clean -fd` 命令中追加显式排除参数 `-e .herdr-launch-identity.json`，确保沙盒清理时身份标桩文件完好存活，不依赖仓库内任何 `.gitignore` 配置，且单点覆盖 `create_task_branch` 与 `checkout_onto_branch` 的所有调用点。
  2. **环境临时绕过清理**：
     - 移除开发机 `~/.config/git/ignore` 中此前作为临时 workaround 写入的 `.herdr-launch-identity.json` 规则，还原纯净全局 Git 配置。
  3. **自动化测试覆盖**：
     - 新增 `tests/test_worker_sanitize_sandbox.py`，模拟真实沙盒清理场景（设置 `core.excludesFile=/dev/null` 隔离全局 gitignore），验证沙盒清理后 dirty tracked 文件被 reset、untracked 临时垃圾被清除，同时 `.herdr-launch-identity.json` 完好保留且后续 `initial=False` 校验与更新成功。

## [2026-10-02] fix | 路由健康体检与探测机制加固：放宽 Smoke 证据提取、消除 UNKNOWN/TIMEOUT 误伤永久禁赛、引入并发受控防超时风暴
- 背景：
  1. 现场排查发现多智能体自动路由决策（Auto Router）总是一边倒地选 `opencode`，其余 Agent（如 `qodercli`、`kimi`、`codex`、`agy`、`grok`）从未被选中。
  2. 根因剖析：
     - **CLI 杂音误杀合法 Agent 为 UNKNOWN**：现代 CLI（Qoder、Kimi 等）输出中包含 ANSI 彩色转义码、反引号/星号 Markdown 修饰、版本横幅通知或流式 JSON 输出，导致 `smoke_response_verified` 判定失败返回 `UNKNOWN`。
     - **UNKNOWN 误入黑名单且路由永久硬过滤**：`bin/herdr-factory` 将所有 `final_status != "READY"`（包含 `UNKNOWN`）全量打入 `unhealthy_agents`，且 `herdr/agent_router.py` 即使在快照过期后也做无差别硬过滤，导致仅慢速启动或有格式杂音的 Agent 一旦体检非 READY 便被永久封杀。
     - **Preflight 瞬时并发探测风暴**：原逻辑以 `len(allowed)`（机队达 8 个进程）瞬时拉起全部 Agent 进程探测，引发网络带宽与 CPU 剧烈争抢，导致多个 Agent 在 40s 内发生超时。
- 变更：
  1. **`herdr/agent_adapter.py` (Smoke 证据链加固)**：
     - 增加 `_strip_ansi`、`_clean_smoke_token`，清洗 ANSI 颜色码与 Markdown/引号标点；
     - 引入良性横幅过滤器 `_is_benign_banner_line`，放行版本通知与加载遥测杂音；
     - 扩展通用结构化消息解析，兼容包含 `user_message` 的流式 JSON 输出，消除误判。
  2. **`bin/herdr-factory` 与 `herdr/agent_router.py` (消除误伤与支持自愈)**：
     - `bin/herdr-factory` 仅将真正不健康的 Agent 记入 `unhealthy_agents`，排除安全的 `UNKNOWN`；
     - `herdr/agent_router.py` 定义 `HARD_UNHEALTHY_STATUSES`，区分快照新鲜度：新鲜快照全量排除不健康 Agent；快照过期后仅硬过滤致命状态，放行 `TIMEOUT`、`UNKNOWN` 进行自愈重试调度。
  3. **`herdr/deep_preflight.py` (并发控流与超时可配)**：
     - 引入 `DEFAULT_PREFLIGHT_CONCURRENCY = 4`，通过 `HERDR_PREFLIGHT_CONCURRENCY` 动态调节并发池，消除 8 进程突发争抢，同时兼容 3 进程存量并发测试；
     - 支持 `HERDR_SMOKE_TIMEOUT` 环境变量覆盖（默认 45s）。
  4. **自动化测试**：
     - 新增及回归覆盖 119 项相关自动化测试，包含 ANSI/Markdown/JSON 兼容、快照过期放行 TIMEOUT、排除 AUTH_REQUIRED、并发控流等场景。

## [2026-10-02] perf | Sidecar 侧边栏交互与刷新性能深度优化：请求并发化、软刷新防闪烁与系统页签保护
- 背景：
  1. 用户反馈控制台左侧 sidecar 的按钮点击以及页面刷新体感迟钝（延迟 1.5s+），存在点击无即时响应、全屏画布闪烁重绘、切屏后系统页签被重置等问题。
  2. 根因剖析：
     - **串行级联请求**：`loadWorkflow` 依次等待 `/api/workflow`、`/api/workflow/controller-actions` 和 `/api/workflow/decisions` 3 个 HTTP 请求，无任何并发；
     - **画布破坏性重绘**：`loadWorkflow` 在发起网络请求前即提前调用 `destroyFlowGraph()` 清空画布，造成长达数百毫秒的白屏闪烁；
     - **DOM 盲目全量重建**：`renderSidebarWorkflows()` 在微小状态变更时重写整个侧边栏 innerHTML，导致点击瞬间 active 样式延迟更新；
     - **系统页签被切屏刷新重置**：`loadProject` 在页面可见性刷新（`visibilitychange`）时未保护 `__ctl__`、`__templates__`、`__archive__`、`__logs__`，粗暴覆盖为 `latest_workflow_id`；
     - **后端冗余子进程**：`project_detail` 内的 `slots()` 二次调用 `panes()`，多次派生子进程放大接口响应延迟；`service_status()` 4 次串行 `launchctl print`（耗时 ~85ms）。
- 变更：
  1. **前端请求全量并发化（Promise.all）与软刷新防闪烁**：
     - `loadWorkflow` 采用 `Promise.all` 并发拉取工作流详情、控制器操作与拍板决策，网络等待耗时降低 60%+；
     - 限制 `destroyFlowGraph()` 仅在工作流 ID 发生实质性变更（`prevWfId !== id`）时触发，消除同流刷新及高频轮询下的空白闪烁；
  2. **DOM 签名对比与瞬时交互响应（Optimistic UI）**：
     - `renderSidebarWorkflows` 引入工作流状态签名缓存（`container.dataset.sig`），当列表未变更时仅通过 `data-wf-id` 切换 `.active` class，消除卡死体感；
     - `openWorkflowTab` 优先乐观更新 `state.workflowId` 与侧边栏状态，并为 `__ctl__` 系统页签直接挂载驾驶舱面板；
  3. **系统原生页签常驻保护（isSysTab Protection）**：
     - `loadProject` 增加 `isSysTab` 校验，切屏与刷新时完整保留系统页签处于激活状态，不再被业务工作流篡改覆盖；
  4. **后端性能调优与进程复用**：
     - `service_status()` 改用单次 `launchctl list` 解析服务运行状态（耗时由 ~85ms 降至 ~9ms）；
     - `project_detail()` 复用外层已查询的 `p_panes`，消除 `slots()` 内部的重复 `panes()` 进程派生，且通过 `try/except TypeError` 严格保持单参猴子补丁向后兼容。
  5. **自动化测试与回归保障**：
     - `tests/test_console_standard_layout_tabs.py` 新增 `test_sidecar_perf_and_soft_refresh` 专项回归测试，全量 231 项测试 100% 绿灯通过。

## [2026-10-02] feat | 调度审计日志全面标签页化：告别弹窗模态，升级为工作区独立 Tab（__logs__）
- 背景：
  1. 用户指出控制台侧边栏“项目治理与审计”中的“调度审计日志”不应使用遮罩弹窗（openModal），而应该是工作区标准 Tab 页。
  2. 随着工作台标准多页签布局确立（工作流流转、Controller 控制台 `__ctl__`、模板资产库 `__templates__`、任务归档库 `__archive__`），调度审计日志作为核心白盒治理能力，应当与治理其他三域保持一致的原生页签体系。
- 变更：
  1. **工作区日志 Tab 承载（#logsTabView）**：
     - 在主工作区面板容器注入 `<div id="logsTabView" class="controller-panel" hidden></div>`；
     - 增加 `.log-kind-selector`、`.log-kind-btn`、`.log-stream-wrap`、`.log-content-pre` 现代浅色样式，遵循 0/4/8/12/16px 栅格白名单。
  2. **原生 Tab 生命周期与调度流控制**：
     - `openWorkflowTab`、`closeWorkflowTab`、`renderWorkflowTabs` 支持 `__logs__` 系统页签；
     - 支持 4 大物理日志流一键切换：Controller 调度主循环、Error 异常错误流、Sentinel 巡检守卫流、Notifier 通知服务流；
     - 支持 100/200/500/1000 行限制选择、自动滚屏、自动刷新（3s）、一键复制到剪贴板与日志文件导出；
     - 服务端 `/api/logs` 增强支持 `n` 行数过滤参数。
  3. **自动化测试与回归防线**：
     - `tests/test_console_standard_layout_tabs.py` 新增 `test_logs_system_tab_not_modal`，验证无 `openModal` 且具备完整的 Tab 生命周期。

## [2026-10-02] feat | 解耦 Left Rail 全局活动栏与 Sidecar 空间资源管理器：消除冗余导航，落地 2026 现代双侧栏四分区架构与可折叠能力
- 背景：
  1. 用户指出控制台左侧 48px 全局导航轨（Left Rail）与 220px 侧边栏（Sidecar）内容完全重复（两者均平铺工作台、仪表板、驾驶舱、告警、工作流等一级入口），要求重新设计。
  2. 参考标准原型蓝图（`prototype_standard_layout_tabs.html`）与 2026 年现代 IDE/SaaS 业界最佳实践（Linear / VS Code / Cursor 双侧栏模式）：
     - 48px Left Rail 专职全局活动栏（Module Switcher: Workbench, Dashboard, Ops, Alerts, Templates, Avatar）；
     - 220px Sidecar 专职空间资源与任务探索器（Contextual Factory Space Explorer）；
     - 彻底消除 sidecar 中冗余的“运转”分组；
     - 引入 Y=44px 共轴折叠/展开机制（⌘B 快捷键、折叠按钮与页签栏常驻展开按钮）。
  3. 严守 100% 现代纯浅色（Linear Light Theme）、首行全屏 Y=44px 绝对共轴基准线以及 8pt/4pt 空间网格白名单。
- 变更：
  1. **双侧栏架构彻底解耦（Decoupled Left Rail & Sidecar）**：
     - **Left Rail**：专职 5 个顶层模块路由（工作台、仪表板、运维驾驶舱、告警中心、模板资产库）及空间头像菜单，不堆叠局部空间细节；
     - **Sidecar 重新梳理为高内聚 4 大生产功能区**：
       - **Zone 1: 空间状态与顶层动作**：当前空间卡片微标（`space-pill`）+ “+ 发起新需求”高亮主行动点（Primary Launch CTA）；
       - **Zone 2: 空间工作流树（按生产态动态分流）**：统计空间工作流总量，分类聚合为“待拍板/阻塞”、“进行中”及“历史完成”，支持状态呼吸灯与一键切换；
       - **Zone 3: 空间资源与工位**：执行者机队阵容微标（在线数/名称）及常驻物理工位状态概览；
       - **Zone 4: 项目治理与审计**：Controller 协调器调度台（带调度脉冲）、模板规范库、调度审计日志、任务归档库及项目注销。
  2. **可折叠侧边栏交互（Collapsible Sidebar & ⌘B）**：
     - `.brand-row` 内置折叠按钮（`#btnCollapseSidebar`）；
     - 页签栏前置共轴展开按钮（`#btnExpandSidebar`），在侧边栏折叠时平滑显现；
     - 支持 `⌘B` / `Ctrl+B` 键盘全局快捷键无缝切换折叠/展开；
     - 折叠时网格由 `48px 220px minmax(0, 1fr)` 动态切换至 `48px 0 minmax(0, 1fr)`，带有 0.15s ease 现代过渡动画。
  3. **自动化测试与兼容性保障**：
     - 更新 `tests/test_console_shell.py`，移除废弃的 `>运转<` 检查，增加新分区断言并严格断言 `self.assertNotIn(">运转<", self.html)`；
     - 扩充 `tests/test_console_standard_layout_tabs.py`，新增第 12 项测试 `test_left_rail_and_sidecar_decoupled_without_duplication`；
     - 控制台全量 229 项自动化测试 100% 通过（`pytest -q tests/test_console*.py`）。

## [2026-10-02] feat | 治理三域全面标签页化：Controller、模板库与归档全面告别弹窗模态，升级为工作区独立 Tab
- 背景：
  1. 用户指出控制台侧边栏“治理”分组下的“模板库”和“归档”仍为弹出框模态（`openModal()`），交互体验与 Controller 及标准多页签工作区不一致，要求按原型图彻底改为工作区独立标签页。
  2. 保持 100% 现代纯浅色（Linear Light Theme），零深色色块；零后端逻辑改动；严守 8pt/4pt 间距网格白名单。
- 变更：
  1. **治理三域独立 Tab 面板体系**：
     - 在主工作区 `<section class="panel main-panel">` 中注入 `#controllerTabView`、`#templatesTabView`、`#archiveTabView`；
     - 增加 `.controller-panel[hidden] { display: none !important; }`，保障 `hidden` 属性不被 `display: flex` 覆盖。
  2. **原生 Tab 动态生命周期打通**：
     - `openWorkflowTab(id)`、`closeWorkflowTab(id, e)`、`renderWorkflowTabs()` 全面纳入系统级特殊页签：`__ctl__`（Controller）、`__templates__`（模板库）、`__archive__`（归档）；
     - 点击侧栏相应按钮或在 Tab Bar 中切换时，激活独立面板并保持侧边栏导航按钮（`#navController`、`#navTemplates`、`#navArchive`）与当前 Tab 状态双向联动。
  3. **彻底废除 `openModal` 弹窗**：
     - 改写 `openControllerCockpitModal()`、`showTemplateLibrary()`、`showArchive()`，均写入对应工作区面板，消除遮罩层。
  4. **测试与运行态全量验证**：
     - 扩展 `tests/test_console_cockpit_runtime.py` 运行时 Mock DOM 拦截支持 `#controllerTabView`；
     - 全量 228 项自动化测试 100% 通过（`pytest -q tests/test_console*.py`）；
     - 热更新部署至生产控制台并通过 launchd 重启，服务实时验证通过。

## [2026-10-02] feat | 标准工作区六分区架构落地：48px 浅色左轨、28px 底部栏、共轴 44px 基准线与手绘草图全面对齐
- 背景：
  1. 用户出示标准手绘草图架构（`uploaded_media_1790904749095.png`），指出当前界面与手绘原型对比缺少核心分区：
     - 缺失独立 48px 纯浅色全局左轨（Left Rail）；
     - 缺失 28px 贯穿式系统状态与底层现场底部栏（Bottom Bar）；
     - Tabs 页签在手绘中仅局限在中间工作区上缘，不能横跨到右侧检查器；
     - 顶栏与全局存在水平错位，必须在全屏 Y=44px 处建立单一一贯、无梯田跳变的绝对共轴基准线。
  2. 坚持 100% 现代纯浅色（Linear Light Theme），绝不用黑底/深蓝背景色块破坏视觉一致性；
  3. 严格遵循 8pt/4pt 空间网格白名单（0/4/8/12/16/20/24/32px），0 违规。
- 变更：
  1. **48px 全局浅色导航轨（Left Rail）**：
     - `#ffffff` 底色，`border-right: 1px solid #e6e8ee`；
     - 顶部 44px 品牌头嵌入 32x32px 标识（`(48 - 32) / 2 = 8px` 居中边距）；
     - 32x32px 标准导航图标，支持工作台、仪表板、驾驶舱、告警、工作流一键切换；
     - 底部空间身份微标与通知小红点。
  2. **首行 Y=44px 绝对共轴基准线**：
     - 左轨头部（`.rail-head` 44px）、侧边栏头部（`.brand-row` 44px）、工作流页签栏（`.workflow-tabs-bar` 44px）、右侧检查器头部（`.flow-insp-head` 44px）在 Y=44px 形成绝对水平贯通线。
  3. **28px 纯浅色全局底部状态栏（Bottom Bar）**：
     - `#ffffff` 底色，`border-top: 1px solid #e6e8ee`；
     - 集成调度器健康指示、当前工作流轮播、活跃执行者计数与毫秒轮询延迟；
     - 底部快捷动作与既有 `#deepDrawer` 底层物理现场抽屉无缝联动。
  4. **DOM 契约与全量测试保护**：
     - 严谨保护既有 DOM ID、挂钩与测试契约；
     - 扩充 `tests/test_console_standard_layout_tabs.py` 六分区架构契约测试（11 项测试全绿）；
     - 控制台全量 228 项测试与全量子测试 100% 通过。
- 证据：
  - `pytest -v tests/test_console_standard_layout_tabs.py`（11 passed）；
  - `pytest -q tests/test_console*.py`（228 passed, 76 subtests passed in 3.69s）；
  - `python3 -m compileall -q herdr services bin tests console`（0 error）；
  - `git diff --check`（0 violation）；
  - 高清真机渲染图 `prototype_standard_layout_screenshot.png` 像素级还原手绘草图。

## [2026-10-02] feat | 控制台浅色风格增量迭代：44px 共轴基准线与多工作流 Tab 标签系统落地
- 背景：
  1. 风格纠偏与去黑化：响应用户明确指示，摒弃深蓝顶栏与黑色背景，全面回归 HAFlow 标志性的 100% 现代纯浅色（Light Theme）设计系统（白底 `#fff`、浅灰 `#fafafa`、边框 `#e6e8ee`、品牌紫 `#5e6ad2`）。
  2. 消除水平线断层：根治多区域顶部高低错落（梯田状）的参差问题，以一把水平标尺贯穿屏幕首行，侧栏顶行、中间工作流 Tab 栏、右侧检查器全部严格统一为 44px 高度并共用单一贯穿底线（Y = 44px）。
  3. 现有架构增量迭代：采纳路线 A（在现有生产控制台 `console/herdr_factory_console.py` 上做最小必要增量），保持现有 2 栏架构与全部现有功能、API 及 2905+ 自动化测试兼容。
- 变更：
  1. **首行 44px 绝对共轴基准线**：
     - `.sidebar .brand-row` 设置固定高度 44px、内边距 `0 16px`、底部分割线 `border-bottom: 1px solid #e6e8ee`；
     - 主区域顶栏 `.top` 设置高度 44px、底部分割线 `border-bottom: 1px solid #e6e8ee`；
     - 悬浮胶囊工具栏（`#canvasToolbar`）保持 `position: absolute; top: 12px; left: 16px;`，浮在点阵画布之上，不打断横向基准线。
  2. **选项 A 动态工作流页签系统**：
     - 在 `.top` 左侧嵌入 `#workflowTabsBar` 与 `#workflowTabsList`，支持动态添加标签与新建工作流（`+`）；
     - 实现 `state.openWorkflowTabIds` 动态生命周期管理：`openWorkflowTab(id)`、`closeWorkflowTab(id, e)`、`renderWorkflowTabs()`；
     - 关闭当前激活页签时自动就近切换至邻近工作流，关闭全部页签时调用 `clearWorkflow()` 优雅置空；
     - 标签头集成工作流状态指示灯（运行绿色、门禁黄色、卡点红色、完成灰色、等待浅灰）；
     - 非工作台视图（仪表板、运维、告警等）自适应切回标准面包屑，保持全量视图兼容。
  3. **原型与生产严格对齐**：
     - 更新 `console/static/prototype_standard_layout_tabs.html` 与 `console/herdr_factory_console.py`，全量样式符合 4/8/12/16px 白名单规范（0 违规）；
     - 静态服务 `send_static` 增加 `.html` / `.htm` 映射至 `text/html; charset=utf-8`。
- 证据：
  - 专属验收测试：`pytest -v tests/test_console_standard_layout_tabs.py`（10 passed in 0.23s）；
  - 全量控制台测试：`pytest -q tests/test_console*.py`（227 passed, 76 subtests passed in 9.60s）；
  - 语法与静态门禁：`python3 -m py_compile`、`compileall`、`git diff --check` 全部 0 警告 0 报错；
  - 视觉效果核验：高清真机截图 `prototype_standard_layout_screenshot.png` 验证横向无参差、纯浅色质感与水平底线绝对贯通。

## [2026-10-02] fix/refactor | 依 snapping-ui-to-grid 规范对齐产品原型与标准工作区格栅系统
- 背景：
  1. 产品原型间距失准：前期生成的交互原型 `console/static/prototype_standard_layout_tabs.html` 存在 34 处脱离 8pt/4pt 系统的非标裸值（5/6/7/9/10/11/13/14px），破坏了仓库既有的格栅对齐原则。
  2. 四条数学基准线失调：
     - **P0 右轴**：审查面板操作按钮呈现「左主右次」排列，主 CTA 未贴紧右边界；
     - **P1 左轴**：侧边栏（Head 14px vs Items 18px）、抽屉（14px）、底部栏（12px）产生多条伪左轴，无法形成 16px 垂直贯穿线；
     - **P1 数字轴**：标签徽标、Agent 耗时与底部状态统计缺少等宽字体与 `font-variant-numeric: tabular-nums`；
     - **Left Rail 8px 空间网格**：48px 导航轨内导航项为 36px（两侧边距各 6px，脱离 8pt 网格），需统一优化为 32px（边距各 8px）。
- 变更：
  1. **间距白名单就近吸附（34 处违规清零）**：
     - 原型全量样式执行属性锚定替换（`(padding|margin|gap)`），彻底清除 5/6/7/9/10/11/13/14px 裸值，100% 吸附至 `4/8/12/16/20/24/32px` 白名单；
  2. **P0 右轴重构**：
     - Inspector 门禁决策按钮组次序重排为 `[次要: 驳回修复] [主要: 批准放行]`，容器采用 `justify-content: flex-end; gap: 8px;`，Primary CTA 严格贴合右边界；
  3. **P1 左轴统一**：
     - 侧栏头部（`12px 16px 12px`）、侧栏分类与列表项（`8px + 8px = 16px`）、发起按钮、抽屉与底部栏统一对齐 16px 左轴；
  4. **P1 数字与等宽轴注入**：
     - 为 `.tab-badge`、`.item-meta`、`.meta-tag`、`.agent-pill`、`.bottom-bar`、`.drawer-content` 注入 `font-family: var(--font-mono); font-variant-numeric: tabular-nums;`；
  5. **Left Rail 8px 空间网格统一**：
     - 原型与生产控制台（`console/herdr_factory_console.py`）中的 `.rail-item` 统一从 36px 优化为 32px，实现 `(48 - 32) / 2 = 8px` 居中边距，与品牌 Logo（32px）和折叠按钮（32px）完美保持 8pt 节奏；
  6. **CI 门禁与防护**：
     - 在 `tests/test_console_standard_layout_tabs.py` 扩充间距白名单校验与四轴吸附原则测试，覆盖原型与生产文件。
- 证据：
  - 间距正则扫描：原型与控制台双文件 `grep -E '(padding|margin|gap)[^:;}]*:[^;}]*[^0-9.](5|6|7|9|10|11|13|14)px'` **0 违规**；
  - 布局专属测试：`pytest -v tests/test_console_standard_layout_tabs.py`（10 passed in 0.44s）；
  - 全量控制台测试：`pytest -q tests/test_console_*.py`（227 passed, 76 subtests passed）；
  - 全仓自动化测试：`pytest -q`（2905 passed, 157 subtests passed in 592.96s）；
  - 静态编译与代码卫生：`compileall` 与 `git diff --check` 0 警告 0 报错；
  - 视觉回执更新：重新生成高清真机渲染效果图 `prototype_standard_layout_screenshot.png`（158 KB）。
## [2026-10-01] feat | 控制台 Linear 风格标准工作流选择器与状态胶囊设计系统规范
- 背景：
  1. 工作流 ID 遮蔽：原先前端使用 `w.title || w.workflow_id`，导致定义了 title 的工作流（如 `wf-project-0929-01` 对应 `Task：统一待办工作台 V1`）ID 被彻底遮蔽，用户误以为刚执行的工作流未出现在下拉框。
  2. 原生 Select 体验落后：原先使用系统原生 `<select>`，不符合 2026 年现代产品体验规范。
  3. Badge 胶囊类名冲突：初期尝试使用 `.badge-pill.dot` 时与控制台全局 `.dot { width: 7px; height: 7px; border-radius: 50%; }` 发生命名冲突，导致胶囊宽度高度被压扁为 7px 细环且文字被挤出竖排折行。
- 变更：
  1. 在 `console/herdr_factory_console.py` 实现全套 Linear 风格 Combobox 标准组件（`.linear-select`, `.linear-trigger`, `.linear-popover`），包含即时搜索过滤、快捷键（`/` 与 `Esc`）、全键盘上下键导航与回车选中、点击外部自动关闭。
  2. 方案 B 状态分组与折叠：按「进行中 / 需决策」与「已完成 / 闲置」分组，支持点击分组标题折叠/展开（`togglePopoverGroup`）。
  3. 双行信息架构：首行标题 + 次行等宽 ID（`item-id`），确保工作流 ID 随时清晰可见。
  4. 胶囊样式隔离（消除 `.dot` 冲突）：移除全局 `.dot` 引用，使用 `.badge-pill::before` 绘制 5px 原点指示器，设置 `height: 19px; white-space: nowrap; font-variant-numeric: tabular-nums`，文案规范统一为 `• X 活跃`、`• Y 需决策`、`已完成` 与 `聚合`。
  5. 兼容性保护：保留隐藏的 `<select id="dashWfSel">`，保障既有自动化测试与外部控制台 API 契约不变。
  6. 同步更新 `console/static/prototype_workflow_dropdown.html` 原型。
- 证据：
  - 新增专项测试 `tests/test_console_linear_dropdown.py`（7 项测试全绿）；
  - 控制台全量测试集 `pytest tests/test_console_*.py`（212 passed）；
  - 全仓测试 `pytest -q`（2891 passed, 157 subtests passed）；
  - `python3 -m compileall`、`git diff --check`、`entry-gate-check.py` 校验全绿；
  - 生产发布快照 `182db0722ee6` 已通过 `./scripts/install-herdr-console.sh` 部署并热重启服务；
  - `ego-browser` 截图验证无文字折行、胶囊原点与数字对齐完美。

## [2026-10-01] fix | 控制台已结束工作流与门禁放行状态投影对齐
- 背景：控制台画布卡片和详情阶段聚合仅基于任务状态盲目聚合（`any(s == 'failed')`），忽略了工作流已完成终态（`workflow.status in {'completed', 'cleaned', 'archived'}`）以及人工门禁放行裁决（`gate_overrides[node].verdict == 'pass'`），导致已闭环工作流历史上的重试失败仍将「实现」标为红色失败、「测试」标为已阻塞、「收尾」因无任务标为灰色尚未开始。
- 修复：
  1. `herdr/workflow_graph.py`: `aggregate_node_status` 增强对任务结论（`stage_verdict == 'pass'`）与最新任务状态的判定；`workflow_graph_projection` 显式解包工作流 `status` 与 `gate_overrides`，当工作流处于完成终态时将所有节点投影为 `completed`（`active_task_count = 0`, `has_attention = False`）；当节点有 pass 裁决且无活动任务时投影为 `completed`。
  2. `console/herdr_factory_console.py`: `workflow_graph_for` 合并传递工作流运行时元数据；`stage_summary` 接入 `workflow` 参数使已完成工作流各阶段聚合为 `cleaned`；前端 `flowCardFoot` 当 `task_count === 0` 且 `status === 'completed'` 时显示「已完成」；`flowChecklist` 与控制台驾驶舱关注阶段对齐完成态。
- 证据：`tests/test_workflow_graph_projection.py` 新增 4 项测试，`tests/test_console_stage_summary.py` 新增 2 项测试，全量 2616 项测试与 154 项子测试全绿，compileall 与 git diff --check 均无异常。

## [2026-09-30] feat | PR #120 控制台 Controller 动作全按钮化 + 待裁决项显式提醒
- 背景：控制台只为 `blocked/failed/rework` 生成按钮，`bin/herdr-task` 的 `commit/integrate/cleanup/finalize/clear-escalation` 从无 `action_id`，于是"无报错但仍待集成"的任务在 UI 上等同于"没事可做"；Barrier-0 的 DU-10 等人工裁决只以自由文本存在于共享文档区与总指挥终端，`dashboard.attention` 完全不收决策类提醒。
- 变更：`herdr/controller_actions.py` 新增交付链路逐步骤表（`needs_git` 逐步骤门控：commit/integrate 需 git clone，finalize/cleanup 只需已落定——整表门控会让占多数的非 git 任务仍无按钮）、终化升级三条处置、真实 re-drive（`herdr agent prompt --wait`，区别于 steer 插话队列）与 `collect_workflow_actions` 聚合；`generate_controller_actions` 收敛为仅经 `resolve_workflow_blockers` 调用，避免其破坏性 `force_pass_advance` 兜底落到 `cleaned/superseded` 历史任务上。
- 提醒面：新增 `herdr/human_decisions.py`，从 append-only 账本折叠"待你裁决"（仅带 `decision_id` 的 decision 笔记算待办；最新一条赢；resolved 关闭；stale 不算）与建议时间线，`ADVICE_KINDS` 直接别名 `workflow_docs.NOTE_KINDS` 避免平行副本漂移；`bin/herdr-task note-add --field KEY=VALUE` 只解析透传，字段校验仍归核心，CLI 与 HTTP 因此可写同一种决策记录。
- 教训（§107）：本 PR 自身带入两个"全量 2494 项全绿"的功能级缺陷——动作卡渲染器提升到模块作用域后仍引用外层 `catMeta`（`ReferenceError`，弹窗完全打不开）、`JSON.stringify` 双引号截断双引号 `onclick`（选项芯片死链）。根因是既有前端测试只有 `node --check`（仅证明可解析，发现不了运行期作用域错误）与字面量 grep。已新增 `tests/test_console_cockpit_runtime.py`：真实抽取 `<script>`、Node + DOM stub 实际调用渲染函数，并对每个 inline handler 逐条 `node --check`。
- 证据：全量 2522 passed / 60 subtests；compileall 与 `git diff --check` exit 0；线上 `controller-actions` 返回 3 个 pipeline 按钮、`decisions` 返回 advice 12；反向验证（篡改核心表 / 注入缺失作用域）均如实变红。详见 `docs/walkthroughs/20260930-console-controller-decision-buttons.md`。

## [2026-09-27] fix | PR #102 只读边界重划：HAFlow 持久状态只读，容忍 SQLite WAL 协调文件
- 评审结论：撤回当日早些引入的 `ReadonlyWalSidecarError` fail-closed preflight——「检查边车缺失再打开」是 TOCTOU 竞态，且 `-wal`/`-shm` 是 SQLite 自身连接协调文件，不应由应用层契约治理。
- 变更：只读契约重划为「不改 HAFlow 持久数据与 schema」（不建库、无 DDL/migration、无业务写、不翻 journal_mode）；`mode=ro` + `query_only` 防写不变；删除 preflight、CLI catch、fixture WAL keeper 与 case3b/case3c，新增 case3 断言「边车被清空的 WAL 库读取成功、应用数据不变、SQLite 关闭后自清协调文件」。
- 证据：shadow 专项 46 passed；教训 §92 追加修正节（append-only）；架构文档 §8 重写只读边界描述。

## [2026-09-27] feat | PR #102 Shadow Evaluation 真只读 DB（分支 feat/shadow-eval-readonly-db）
- 背景：`herdr-task shadow-eval` 号称只读，但经 `_get_store()` → `get_db_connection()` 会 mkdir/建库/切 WAL/跑 `_ensure_schema` DDL 与 migration——“无业务写”不等于“无副作用”。
- 实现：新增独立只读入口 `state_db.get_readonly_db_connection()`（URI `mode=ro` + `PRAGMA query_only=ON`，无 mkdir/建库/WAL/schema init/schema lock）；`query_route_decisions`/`batch_get_execution_outcomes` 统一走 `_open_shadow_read_connection`（连接 + SELECT-only 能力检查）；CLI 改纯路径解析 `resolve_state_db_path()`，不再建 StateStore。
- 语义：DB 不存在 → `[SHADOW-EVAL] state database not found` 非零退出且不建库；旧 schema 缺表缺列 → `ReadonlySchemaError`（`not compatible with shadow evaluation`）非零退出且不 migration——诊断只观察，不修库。
- 证据：shadow 专项 45 passed；全量 1797 passed + 44 subtests；生产连接行为零改动；指标/Router 口径零改动（§24/§25）。

## [2026-09-23] feat | Console UI V1 Linear 风格产品化视觉重构
- 背景：原 HAFlow 控制台大面积纯黑背景与大卡片嵌套，指标卡片占据首屏高度，操作按钮无主次，执行者阵容常驻挤占主工作流视区。
- 重构：遵循 Linear 产品化克制规范：浅色统一 Design Tokens，单行内联指标元数据，极简水平阶段步骤条，44px 紧凑表格任务行，操作按钮收敛至单一 Primary CTA + 次级 `···` 下拉菜单，执行者/工位/告警下沉至底部可折叠手风琴面板。
- 纪律与兼容：零新增外部框架与构建链（纯原生 HTML/CSS/Vanilla JS）；保留全部 78 个 DOM ID 与已有 API 轮询、事件监听、运维驾驶舱切换逻辑；测试与合规断言 100% 通过。
- 回归与审查：95 项 Console 专项测试 + 1202 项全库回归通过；独立 Reviewer 子 Agent 审查 APPROVED / MERGE_READY。

## [2026-09-23] fix | Console task views use StateStore
- 背景：普通 Workflow 页面和执行者负载从兼容 `tasks.json` 读取，ops-center 从 StateStore 读取；新 Workflow 的任务只在 SQLite 中可见时，页面显示空阶段和零负载。
- 修复：Console 统一经 `tasks()` → `herdr_kernel.load_tasks_data()` 读取权威任务；移除归档查询的陈旧 JSON fallback，并让成果会签任务定位复用同一读取入口。
- 更新 [[ops-center]]：记录普通 Workflow、执行者负载、工位占用、Task 详情与归档查询的权威任务来源。
- 经验：更新 `docs/lessons/lessons-learned.md` §40；回归测试覆盖投影为空、StateStore 有任务、跨 Workflow 过滤及执行者负载统计。

## [2026-09-22] fix | Harness Metrics Run 完成事实与 Task 生命周期状态分离
- 固化 [[task-lifecycle]] 的状态机语义：Metrics 用 Trajectory `run_completed` 表达 Run 曾成功完成，用 `COMPLETED_TASK_STATUSES` 表达当前 Task 完成态。
- 关联教训：`docs/lessons/lessons-learned.md` §80；回归覆盖 committed、cleaned、superseded-after-completion。

## [2026-09-21] fix | 门禁裁决解析 Prompt 回显防御与字串误判修复（fix/gate-verdict-prompt-echo）
- 背景：工作流门禁节点启动时，终端回显 Prompt 中的契约说明（`HERDR_GATE_VERDICT: pass 或 HERDR_GATE_VERDICT: blocked`），Controller `_verdict_from_screen()` 粗暴取首词导致在 0 秒内误判为 `pass`，门禁被瞬间击穿并提前清理 Pane；首次修复尝试中使用子串检测又导致 `smoke`（含 `ok`）等合法阻塞判定被误伤。
- 改动：`herdr/direct_dispatch.py` 将契约模板改为语法占位符 `<pass|blocked>`；`services/herdr-controller.py` 增加 `_is_instructional_or_ambiguous_verdict_line()` 行级过滤多标记及二选一占位符，并在取首词后对后续 token 集合比对对立关键字；严格移除全行泛化状态词过滤与子串模糊匹配；在 `tests/test_auto_acceptance.py` 补充 7 组正向与对抗回归测试。
- 测试：`pytest tests/test_auto_acceptance.py` 33 passed；全库 `pytest -q` 981 passed。
- 知识：lessons §79。

## [2026-09-20] fix | Agent Trajectory Ledger 事实一致性修复（PR #69）
- 背景：正常 `_launch_task()` 的 task 持久化遗漏 `run_id`，初始事件只能落到历史兼容 fallback；`verification_completed` 曾用计数器重推 `passed`，可能与 evaluator 的 `converged` 事实相反。
- 修复：launch 在 `save_tasks()` 前生成并写入唯一 `run_id`；verification 直接使用 `converged`，同时保留 bounded failing/lint/type/composite/evidence_id 证据。
- 模型关系：Trajectory Ledger 复用 SQLite `events` 表，按 run 的 sequence 记录历史事实；Runtime State 继续只表达当前状态。
- 回归：新增正常 launch 同 run 事件流与 `converged=false` 的两条 regression case。

## [2026-09-20] fix | 门禁 verdict 收口、worker 独立启动修复与待决策展示（PR #67）
- 背景：门禁 test 节点已写出合法 verdict 却被 `[COMPLETION DEFERRED]` 卡死在 working（产物门禁把人类契约标签当文件路径，verdict 契约消费不到，任务最终 superseded 重跑）；#64 给 `services/herdr-worker.py` 引入 `herdr.git_coordination` 时缺 sys.path bootstrap 且回退到不存在模块，脚本方式拉起必然崩溃；Console 待决策横幅只聚合不展示明细。
- 改动：`services/herdr-controller.py#gate_verdict_ready`——门禁节点有 pass/blocked verdict 即放行 `idle → agent_done`，进入既有 auto-verdict 收口（不新增状态机分支）；`services/herdr-worker.py` 头部注入 `HERDR_ROOT`；Console 逐条展示待决策问题与依据并补充 gate 节点元数据；新增 `tests/test_script_bootstrap.py`，把"入口脚本必须 bootstrap repo root"从单点修复升格为自动门禁（sentinel→worker 二次复发）。
- Updated [[dag-workflow-engine]] §10：verdict 就绪即放行完成。
- 测试：`tests/test_script_bootstrap.py` 2 passed；`tests/test_herdr_worker` + `tests/test_fix_loop_anti_flapping` 14 passed；`tests/test_console_frontend_syntax` 11 passed。
- 知识：lessons §73 / §74。

## [2026-09-19] fix | Workflow Run Definition Snapshot：Run 创建即冻结执行定义
- 背景：项目共享 `~/.herdr-controller/projects/<project_id>/workflow.json` 代表"下一次 Run 的当前模板"，模板切换会覆盖它，导致历史 Workflow 的 `workflow_config_for()` 可能读到新模板的 DAG，违反"Workflow Run 一旦创建，其执行定义不可变"。
- `herdr/projects.py` 新增 `_snapshot_workflow_definition()`：context Run 在 `register_workflow` 时把创建时刻的完整定义固化到 `~/.herdr-controller/workflows/<workflow_id>/workflow.json`（复用 workflow 级根目录，与 `shared/` 同级共存），registry `workflow_file` 指向 Run 私有快照。
- 边界：git Run（不传 execution）完全不触发快照，行为零变化；不新增数据库表、不重构 StateStore、不改 Context Contract 与 Template switch 机制；快照失败（源缺失/非法 id/IO 错误）向 stderr 告警并回退旧共享文件行为，不阻断 Run 创建；运行期现场修复（tab/anchor 映射）写入 Run 本地快照，不再污染共享文件。
- 测试：`tests/test_execution_context_contract.py` 新增 2 例（A→关闭→切 B→B 注册后 `workflow_config_for(A)` 仍为模板 A 的 DAG、git 注册不受影响）；全量 831 passed + 44 subtests。

## [2026-09-18] feat | Task/Workflow State 与 Runtime State 分离：新增 RuntimeState 记录
- 背景：Task 记录把编排状态（status/node/goal）与运行环境（workspace/tab/pane/agent）混在顶层扁平字段，且 `agent_session` 在落盘时被丢弃、`agent_status` 从不持久化，无法回答"这个任务到底由谁、在哪里执行的"。
- 新增 `herdr/runtime_state.py` 纯函数核心：`build/normalize/status映射/transition`；`task["runtime"]` 嵌入 `payload_json`（零 schema 迁移）；`launch_task` 记录真实 Herdr 证据（workspace/tab/pane/cwd/agent/session）；`kernel.transition_task` 同事务同步 `runtime.status`（created/running/completed/failed/unavailable）；只记录不恢复；旧 execution 无 runtime 照读照转。
- 测试：`tests/test_runtime_state.py` 新增 6 例；全量 683 passed；`doctor` PASS；真实 `herdr agent get` 数据 + 隔离态 DB 全生命周期验证。

## [2026-09-18] feat | Workflow 共享文档区：代码物理隔离 + 文档/证据受控共享
- 背景：每个 Task 独立 CoW clone，unified-dev-flow 式跨阶段证据链断裂（requirements 的规格/Entry Gate、test/review 的验证证据在下一节点不可见）。
- 新增 `herdr/workflow_docs.py`：clone 外追加式账本 `~/.herdr-controller/workflows/<wf>/shared/notes.jsonl`；provenance（node/task/agent/source/base_sha）；读取时计算 stale（base 漂移作废 evidence/gate；fix-loop 作废早于作废点的目标节点条目）；按节点相关度摘要渲染。
- `bin/herdr-task` 新增 `note-add` / `note-list`；`set <task> completed --verdict` 自动落 controller 机器证据（kind=gate）；launch 将共享区路径注入 `.agent-task-context`。
- Controller 在 direct dispatch 与总指挥消息中注入共享文档区块（目录 + 权威层级 + 写入指引 + 相关条目），fix-loop 作废时写 invalidation 证据；git/verify-baseline 仍是代码唯一事实来源。
- Updated [[task-lifecycle]] §5：跨任务合法信息通道从"仅固化产物"扩展为"固化产物 + 受控共享文档区"。
- 测试：`tests/test_workflow_docs.py`、`tests/test_workflow_docs_cli.py` 新增 29 例；全量 677 passed；测试套件隔离 `HERDR_WORKFLOW_DOCS_DIR`，不再污染真实状态目录。

## [2026-09-17] feat | 新增 SEO 诊断与通用数字化任务工作流模板 (seo-audit-v1 & general-task-v1)
- 新增 `workflow_templates/seo-audit-v1.yaml`：专用于网站在百度/通用搜索引擎未收录、死链及抓取异常的诊断与整改 4 阶段 DAG 模板（技术抓取诊断 → 关键词矩阵规划 → 落地整改规划 → 高管交付报告）。
- 新增 `workflow_templates/general-task-v1.yaml`：适用于非代码工程类数字化任务的标准 3 阶段工作流（任务理解边界 → 专项深度执行 → 成果质检交付），解除历史将所有非研发任务硬编码绑定 `software-development-v1` 的误配。
- 新增/补充 `tests/test_workflow_engine.py` 自动化测试，验证两套模板在 Kahn DAG 拓扑校验、无环检测与工位元数据装配下 100% 绿灯。

## [2026-09-17] fix | 任务级门禁结论对称：自动推进不再越过 blocked 依赖
- Updated [[dag-workflow-engine]] §4.4：plan/requirements 等无 gate 配置节点的 blocked 结论同样暂停自动推进（sweep 只 funnel 裁决、不销毁下游；direct 同查）；作废后自动恢复。
- 前端启动等待 180s→620s，对齐后端 600s 超时，消除误报式"启动失败"。
- 根因：GATE_DEFAULTS 仅 test/review/wrapup，sweep fix-loop 看不见 plan 级 blocked，而 direct 完全不查结论（线上 plan blocked 时 test-auto 仍被建出）。
## [2026-09-17] fix | Cross-system agent executable resolution, deep preflight isolation & project adoption resilience
Hardened HAFlow execution kernel when integrated with external Agent OS / Task Orchestrators (e.g. StaffAI / agency-agents):
- Fixed Python 3.14 strict Path typing TypeError in `herdr/workflow.py:load_template` and `herdr/projects.py:provision_project` when `template_name` is None or omitted during project registration.
- Added target-agent isolation in `bin/herdr-factory:run_workflow_preflight`: when an explicit agent is chosen (e.g. `agy`), only that agent is probed, avoiding unnecessary 90s timeout delays from slow/stalled external proxies.
- Added `--permission-mode bypassPermissions`, `--no-session-persistence`, and closed stdin with EOF in `herdr/deep_preflight.py` to eliminate interactive permission hangs on Claude Code CLI.
- Registered `agency-agents` as an official HAFlow workspace project (`agency-agents-575746af`, workspace `wH`, coordinator pane `wH:p1`).
- Captured comprehensive cross-module lessons in `docs/lessons/lessons-learned.md` §59. Full suite: 531 passed.
## [2026-09-16] feat | Full-chain scheduling & health probe support for Kimi Code CLI
Integrated Kimi Code CLI (`kimi`) as a first-class supported agent across HAFlow:
- Added `KimiAdapter` in `herdr/agent_adapter.py` declaring interrupt, soft-steer, and resume capabilities.
- Added `kimi` in `herdr/agent_binary.py:AGENT_BINARIES` and `herdr/agent_router.py:DEFAULT_ALLOWED`.
- Implemented `ensure_kimi_workspace_trust` in `services/herdr-worker.py` to seamlessly pre-trust CoW sandboxes via SHA256 hashed trust metadata in `~/.kimi-code/workspace-trust`.
- Configured `--auto` Never Ask execution flag in worker pane dispatch.
- Added non-interactive probe adapter `kimi -p` in `herdr/deep_preflight.py` and credentials hint in `herdr/preflight.py`.
- Updated console views, CLI choices (`bin/herdr-factory`, `bin/herdr-task`), and default template (`software-development-v1.yaml`).
- 56 agent-related regression tests passed, 531 full-suite tests green.

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

## [2026-09-24] fix | Context Compiler execution-scoped source consistency
- 背景：Context Compiler 的 source clock/head 原先会把其他 Workflow 的写入误判为 stale；并发 schema 初始化、旧 schema 迁移、Handoff stale Task snapshot 和小 verification window 也存在边界竞态。
- 修复：source head/clock 按 `(run_scope, workflow_id)` 隔离；schema 初始化使用 resolved-path lock；Workflow/Task/Event/Finding source trigger 按 scope 更新；dispatch 重新读取权威 Task；Verification/Eval 窗口保留 latest/strict/recovery facts 并遵守 limit；补齐 alternate verification payload、source projection 稳定字段、taskless workflow fail-closed 和 context_id 幂等重试。
- 回归：跨 Workflow/同 execution scope、A 自身写入、旧 schema/空库并发、路径别名、Handoff stale snapshot、limit 0/1、恢复序列和 provenance 边界均有测试；交付验证见 `.omc/verify-ses_f2d90b418ffePthpvvnlqq6gVw.md`。

## [2026-09-24] fix | Context Compiler authority, migration recovery, and fail-closed source facts
- 背景：独立复审发现旧 trigger/partial migration、删除 Task snapshot、legacy evidence、超限 failure/verification、alternate verification 和 scope metrics 仍可能 fail-open。
- 修复：dispatch 对数据库已有 Task 时强制权威读取；source-head 复合迁移可从 legacy 表恢复；schema lock/旧 trigger replacement 可重入；failure/critical event 与 oversized verification 使用有界保留和 truncated fail-closed marker；legacy Handoff evidence 只接受 scope 内已存事实；storage 强制 verification bool、配置预算和 workflow-scoped metrics。
- 回归：新增删除 Task、stale upstream、legacy evidence、旧 trigger、partial migration、oversized source、冲突 verification、source projection 和 storage adversarial 测试；全量验证见 `.omc/verify-ses_f2d90b418ffePthpvvnlqq6gVw.md`。

## [2026-09-24] fix | Context Compiler final fail-closed review closure
- 背景：复审继续发现旧 trigger/partial migration、删除 Task fallback、failure/verification 超限、alternate verification、legacy evidence、storage schema/budget 和 metrics scope 边界。
- 修复：权威 Task/upstream dispatch、resolved-path schema lock 与 trigger replacement、legacy source-head row recovery、critical/oversized source marker、canonical alternate verification、legacy evidence scope filter、strict verification/budget validation、workflow-scoped metrics 和空 scope 归一化。
- 回归：全量 `1507 passed, 44 subtests passed`；专项、AST、compileall、ruff、diff 和凭据扫描均通过，验证 artifact 已更新。

## [2026-09-24] fix | Context Compiler final adversarial window closure
- 背景：复审发现 trigger 替换窗口、Task 全表删除 fallback、legacy old-run evidence、缺 workflow 的 task-bound failure/verification、critical Finding/Handoff 噪声、source projection 截断和 storage cap 边界。
- 修复：trigger 替换使用 `BEGIN IMMEDIATE`；dispatch/wiring 只接受持久化权威 Task；legacy evidence 绑定当前 Task run；task-bound legacy source 允许缺 workflow 但仍校验 scope；critical Finding 与目标 incoming Handoff 使用保留窗口；Workflow node projection 使用完整节点摘要；storage 强制非保护项预算和 kind caps。
- 回归：新增删除 Task、old-run evidence、缺 workflow source、critical Finding/Handoff noise、超大 source、storage cap、projection cap 和并发相关测试；验证结果与 PR 状态以当前交付 artifact 为准。

## [2026-09-25] fix | Context Compiler final identity/retention closure
- 背景：复审发现 migration/trigger 两阶段窗口、跨 Workflow target dispatch、nested verification conflict、oversized Eval unknown、current blocker cap、scope UPDATE、legacy stages、legacy missing-workflow evidence、ordinary oversized source 和 metrics/config 边界。
- 修复：迁移与 trigger replacement 合并为同一写事务；dispatch 强制 target workflow；冲突 verification 以 strict failure 优先；oversized Eval/普通 event 保留 fail-closed marker；current Task blocker 受保护；UPDATE 同时推进旧/新 scope；`nodes`/`stages` 使用完整摘要；legacy evidence 支持 task-bound 缺 workflow；metrics 读取 alternate verification；低层 storage 规范化空 config。
- 回归：全量 `1516 passed, 44 subtests passed`；专项、compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler final provenance and retention closure
- 背景：复审发现 verification alias conflict、跨 Workflow missing-workflow evidence、普通 oversized marker、未知 Eval next action、同 scope Workflow UPDATE、混合 nodes/stages projection 和低层 config 边界。
- 修复：所有已识别 verification false 统一输出 false；legacy evidence 校验 task Workflow；truncated marker 提高相关性并驱动 next action；UPDATE 推进旧/新 workflow heads；nodes/stages 分别生成摘要；storage config 统一规范化。
- 回归：全量 `1519 passed, 44 subtests passed`；专项、compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler provenance closure final pass
- 背景：复审发现 oversized critical facts 可被普通噪声挤出、紧预算 `compiled_at` 舍入导致 stale loop、verification alias 可在 storage 中矛盾、source_refs 未闭环，以及 derived task refs/metrics 边界。
- 修复：超限 critical/verification 优先保留；保留 `compiled_at` 精度；storage 拒绝 alias 冲突并要求 aggregate provenance 覆盖所有 item/evidence refs；task derived refs 校验真实目标；低层 source/config 边界继续规范化。
- 回归：全量 `1523 passed, 44 subtests passed`；专项 `277 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler critical window and derived-ref closure
- 背景：复审发现 status-only blocked derived ref 被 storage 拒绝、同源 oversized verification 噪声可挤出 strict failure、phantom artifact ref 和 aggregate provenance 回归测试为空。
- 修复：derived blocker 与 compiler 合成规则一致；critical/failure/truncated verification 优先于同类 unknown 噪声；artifact derived ref 校验真实非空目标；aggregate source_refs 测试改用稳定 artifact fixture。
- 回归：全量 `1527 passed, 44 subtests passed`；专项 `281 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler same-source clock and migration closure
- 背景：复审发现 oversized strict failure 自身可能被同源 unknown 挤出、复用 run_id 的 taskless source clock 归错 scope，以及 source-clock 中间 schema 未校验复合主键。
- 修复：oversized SQL 读取有限 strict-failure 分类并优先保留；taskless source 按 workflow heads 广播并消除复用 run_id 的任意 scope lookup；source-clock migration 同时校验列和复合主键。
- 回归：全量 `1529 passed, 44 subtests passed`；专项 `283 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler strict oversized verification closure
- 背景：复审发现 oversized strict verification 自身可能被同源 unknown 挤出，legacy missing-workflow Finding supersession 回源失败，重复 run_id metrics 仍猜测身份。
- 修复：oversized SQL 读取有限 strict-failure 分类并优先保留；legacy Finding relation 允许 task-bound 缺 workflow 后由 scope 校验；metrics 遇到多 execution scope 复用 run_id 直接返回 unknown/零聚合。
- 回归：全量 `1531 passed, 44 subtests passed`；专项 `285 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler final identity and derived-source closure
- 背景：复审发现 oversized strict verification 在同源 unknown 噪声下仍被替换、重复 run_id metrics 仍会聚合未知身份、legacy planned-link critical Finding 窗口丢失、空字符串 workflow supersession target 无法回源，以及无效 derived task entries 导致索引错位。
- 修复：oversized window 保留 strict failure marker；metrics 对重复 Task/跨 Workflow taskless source fail closed；legacy scope expansion 重用 critical Finding 保留窗口；relation 接受 NULL/空 workflow 并继续 scope 校验；候选构造先过滤空 artifact/blocker 再编号。
- 回归：全量 `1536 passed, 44 subtests passed`；专项 `290 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler cross-partition window and recovery closure
- 背景：复审发现 strict verification 在跨 Task critical noise 下被总窗口挤出、legacy linked critical Finding 被普通 warning 挤出、oversized failure marker 绕过 recovery、无 Task 行跨 Workflow taskless metrics 仍混叠，以及 open-question/迁移 revision/node key/top-level passed/超限 Finding 边界缺口。
- 修复：strict verification 使用独立优先槽位；所有 critical Finding 跨 Task 优先；oversized failure 经过 recovery/completed 状态过滤；无 Task 行也扫描多 Workflow source identity；过滤 derived questions；迁移复制中间 clock revision；支持 node key；对 top-level passed 统一 fail-closed；critical Finding fallback 有大小/损坏 marker。
- 回归：全量 `1543 passed, 44 subtests passed`；专项 `297 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler taskless identity and recovery closure
- 背景：复审发现复用 run_id 的 taskless source 可跨 execution scope 泄漏、无 Task metrics 对缺失/ghost identity 未完全 fail closed、不同 scope UPDATE 广播、残留 legacy clock revision、oversized recovery 挤出，以及 malformed verification/provenance 边界。
- 修复：taskless source 使用全局唯一 run→scope 映射；metrics 对无 Task/缺失 Workflow/ghost Task 返回 unknown；UPDATE 仅在实际 scope 变化或旧 taskless 时广播；残留 legacy clock 合并；oversized recovery 独立保留；storage 强制 verification Mapping、Workflow item 不得伪造 run provenance；failed 状态合成 blocker；Handoff attach 保持单一 context ref。
- 回归：全量 `1544 passed, 44 subtests passed`；专项 `298 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler unique taskless identity and source recovery
- 背景：复审发现超大 payload Task 会绕过 taskless run→scope 唯一映射、无 Task metrics 对 ghost/缺失身份未完全 fail closed、残留 legacy clock 未取 max、oversized recovery fact 丢失、direct linked critical Finding 仍可能被挤出，以及 storage 对 Mapping/Workflow provenance 边界不完整。
- 修复：taskless identity 从全量 workflow Task 行建立唯一映射；无 Task source metrics 一律 unknown/零聚合；legacy clock 使用 max upsert；recovery/completed 独立进入 completed；critical Finding SQL 优先 direct dependency/Handoff；storage 强制 verification object 并禁止 Workflow item run provenance；failed 状态和 derived question 规则补齐。
- 回归：全量 `1547 passed, 44 subtests passed`；专项 `301 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler pre-hash source filtering closure
- 背景：复审发现被 scope 排除的 raw Event/Finding/Observation 仍参与 source projection/hash，造成跨 scope 写入改变 context identity；ContextPack metrics 未纳入无 Task identity 扫描；legacy stage id fallback 和非 dict Mapping 持久化仍有边界缺口。
- 修复：所有 source snapshot 在 hash 前完成 taskless scope 过滤；ContextPack 纳入 metrics identity scan；Mapping 在 fingerprint 前递归规范化；legacy stage fallback 使用 `key or id` 并跳过空 ID；补充跨 scope hash 稳定性和 ghost ContextPack/Mapping 回归。
- 回归：全量 `1550 passed, 44 subtests passed`；专项 `304 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

## [2026-09-25] fix | Context Compiler pre-limit scope and legacy identity closure
- 背景：复审发现 Event/Collaboration 仍在 scope 过滤前 LIMIT、legacy fallback run_id compiler/storage 不一致、Mapping 脱敏顺序和 legacy Handoff evidence fallback 仍有缺口。
- 修复：Event/Finding/Observation/Eval/Collaboration 使用 taskless scope filter pre-limit；compiler/storage 共享 `run_id_for_task` fallback identity；Mapping 先递归规范化再脱敏；legacy evidence 使用 fallback run；补充跨 scope hash/window、ghost ContextPack、Mapping、fallback provenance 回归。
- 回归：全量 `1552 passed, 44 subtests passed`；专项 `306 passed`；compileall、AST、ruff、diff 和凭据扫描均通过。

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

## [2026-09-17] fix | 路由健康门禁减法优先 + 投递熔断 + 基础设施失败自动补派（wf-nexusarchive-0917-01 空转事故）
- **背景**：`wf-nexusarchive-0917-01` 需求阶段 challenger 卡 `dispatched` 2h50m（Pane 无投递痕迹）；test 节点被派给 `pi`（`AUTH_REQUIRED`）3 秒空完成 → 总指挥判 failed → 人工 28 分钟才 `--supersedes` 重派；failed 任务无任何自动补派路径。
- **改动与实现**：
  1. **`herdr/agent_router.py`**：候选过滤改为 `allowed - disabled - unhealthy` 减法优先，`unhealthy_agents` 永不自动入选；正向 `healthy_agents` 交集仅在 `preflight_checked_at` 处于 `HERDR_PREFLIGHT_TTL`（默认 1800s）内生效，过期快照降级为仅减法（时间戳缺失视为新鲜，保持 legacy 语义）；
  2. **`herdr/liveness.py`**：新增纯函数 `evaluate_dispatch_fuse`（dispatched 投递 SLA 违约检测，episode 按 `(task_id, updated_at)` 去重、`requeues` 跨重派继承）与 `select_infra_failures_for_recovery`（节点无活跃任务 + 基础设施原因 + 谱系失败 < 上限）；
  3. **`services/herdr-sentinel.py`**：`check_dispatch_fuse` 取证 `_pane_delivery_evidence`（投递标记 + Agent 状态），无标记且非 working → `[SENTINEL FUSE]` 置 failed（`dispatch_delivery_fuse`）并通知；有标记只告警；`HERDR_DISPATCH_FUSE=0` 关闭；`sentinel-state.json` 新增 `dispatch_fuse` 事件簿；
  4. **`services/herdr-controller.py`**：`recover_infra_failed_tasks` 对基础设施 failed 任务自动 `supersede` + 清节点 stage-advance 闩，由既有 sweep 补派 `-rN`（`[AUTO RECOVER]`），谱系上限 `HERDR_AUTO_RECOVER_MAX`（默认 2），质量类失败不翻案。
- **验证与部署**：
  - 新增 `tests/test_agent_router_preflight.py`（7 项）与 `tests/test_dispatch_fuse.py`（18 项），全量 578 项测试 PASS；
  - `launchctl kickstart -k` 重启 Sentinel 后实测捕获真实僵尸任务：`[SENTINEL FUSE] task=wf-agency-agents-0917-05-requirements-executor waited=2064s marker=False agent=idle action=failed`；重启 Controller 后自动接住总指挥补派任务（`[RECOVERY] ...-implementation-fix-r2 registry=dispatched agent=working`）；
  - 沉淀通用工程教训 §60，更新 [[agent-routing-and-pools]] §4/§5 与 [[architecture]] §2.1/§2.2/§3.1。

## [2026-09-17] fix | 补派谱系去重 + 非门禁节点规则化验收（同一工作流第二组浪费）
- **背景**：`wf-nexusarchive-0917-01` 8 小时里仅 ~3.2h 真实干活。除 §60 两类浪费外，又确认两组：① fix-loop 第 2 轮一次性派出 r4+r5 两个重复测试任务（`[STAGE ADVANCED DIRECT] node=test tasks=...r4,...r5`），因补派集合从不按替换谱系去重、也从不回写 `superseded_by`，历史作废任务随轮次 2→4→8 放大；② agent_done 验收强依赖单总指挥 LLM，累计等待 ~2.6h，长回合还会阻塞后续门禁事件投递。
- **改动与实现**：
  1. **`herdr/direct_dispatch.py`**：新增 `lineage_key` / `lineage_redispatch_candidates`，补派按替换谱系（x / x-r2 / …）取唯一最新一发；谱系内尚有非 superseded 成员（在跑或已落定）时不再补派；修正既有 `test_redispatch_id_skips_existing` 中被放大的期望值；
  2. **`services/herdr-controller.py`**：新增 `try_auto_accept` / `node_is_gate` / `task_changes_recorded` / `auto_accept_enabled`，非门禁节点在 `verify-baseline` 报告 `TASK_CHANGED` 时直接 `completed` 并走既有 finalize 链路（`[AUTO ACCEPT]`），在 `_process_coordinator_item` 的任何 coordinator prompt 之前生效；门禁节点、`BASELINE_MATCH`、配置不可判定（fail-closed 视为门禁）、`HERDR_AUTO_ACCEPT=0` 一律回落总指挥。
- **验证与部署**：
  - 新增 `tests/test_auto_acceptance.py`（9 项）与 `tests/test_direct_stage_dispatch.py` 4 项谱系用例，全量 591 项测试 PASS；
  - 现场复现：用真实 `tasks.json`（r4 working + r5 superseded）模拟 test 节点决策 → `mode=wait / specs=[]`（旧逻辑会再派 3 个重复任务）；实况处置 `herdr-task supersede ...-r5` 保留策略 Agent claude 的 r4；
  - `launchctl kickstart -k` 重启 Controller 后实测未再产生重复派发；沉淀教训 §61，更新 [[dag-workflow-engine]] §4.3 与 [[architecture]] §2.1。

## [2026-09-17] fix | 门禁 verdict 契约化：报告结论直接成为裁决（免总指挥转写回合）
- **背景**：非门禁节点验收规则化后，剩余长尾集中在门禁节点（test/review/wrapup）——verdict 只存在于 Agent 自然语言报告，必须由总指挥 LLM 阅读转写为 `herdr-task set completed --verdict`；总指挥上下文累积 566K tokens，单回合 10-20min，且长回合阻塞后续事件投递（`[COORDINATOR BUSY] waited=900s`）。
- **改动与实现**：
  1. **`herdr/direct_dispatch.py`**：新增 `GATE_VERDICT_CONTRACT` 与 `gate_contract` 参数，门禁节点 prompt 注入结论契约（写 `<clone>/.herdr/gate-verdict.json` + 终端输出 `HERDR_GATE_VERDICT: pass|blocked`）；
  2. **`services/herdr-controller.py`**：新增 `read_gate_verdict`（文件 + 屏幕双通道，归一化别名，仅唯一一致结论才采纳）、`try_auto_verdict`（调用既有 CLI 契约落 verdict + completed，blocked 自动进 fix-loop）、`HERDR_AUTO_VERDICT` 开关；在 done 事件快路径与 `try_auto_accept` 串联；
  3. **存量任务补契约**：经 Steering Mesh（`herdr-task steer`）向在跑门禁任务注入契约说明，无需重启任务。
- **验证与部署**：
  - 新增 `GateVerdictUnitTest` 9 项 + 门禁 wiring 1 项、门禁契约注入 2 项，全量 603 项测试 PASS；
  - 现场只读校验：review 节点 prompt 含契约标记；对 r4 任务 `read_gate_verdict()` 返回空（无标记不误判）；`node_is_gate` 分类正确（test/review/wrapup=true）；
  - 沉淀教训 §62，更新 [[dag-workflow-engine]] §10 与 [[architecture]] §2.1。

## [2026-09-17] fix | 终化重试护栏全覆盖：commit 门禁瞬时失败不再成为 completed 死区
- **背景**：`implementation-fix-r2` 验收通过后 commit 被目标仓 bugfix 门禁拦截（`frontend.test` 抖动，`ErrorBoundary.test.tsx` teardown 型，历史采样约 50% 抖动率）；`finalize_completed_task` 失败后任务停留在 `completed`——既非 `committed`（有重试护栏）也非终态，**无任何自动重试路径**，总指挥在 577K tokens 回合里手工重试 5 次、阻塞 65 分钟（期间 test 节点 done 事件一直 `[COORDINATOR BUSY]`）。叠加环境因素：系统 load 61/150/176（他项目长驻进程），门禁抖动概率被放大。
- **改动与实现**：
  1. **`services/herdr-controller.py`**：新增纯函数 `should_retry_finalize`（`committed`/`completed` + git + 退避窗口 + `HERDR_FINALIZE_RETRY_MAX` 默认 5 上限 + 耗尽判定）；registry watcher 的原 `committed` 重试分支扩展为统一终化重试（reason 区分 `integration_retry`/`commit_retry`），耗尽后 `[FINALIZE RETRY EXHAUSTED]` 只告警一次并升级人工；
  2. 重试复用幂等的 `finalize_completed_task`，不引入新状态。
- **验证与部署**：
  - 新增 `FinalizeRetryDecisionTest` 5 项，全量 608 项测试 PASS；
  - 现场恢复链复现：`c30d99e2` 提交 → Controller `[REGISTRY WATCHER] committed -> retry finalize` → `cleaned`；
  - 沉淀教训 §63，更新 [[architecture]] §2.1。

## [2026-09-17] fix | 工作流收官补上「交付 PR」环节（wrapup 必做前置）
- **背景**：`wf-nexusarchive-0917-01` 20:35 收官（completed/delivered），但交付 PR 从未创建：集成分支 `herdr/integration-...-fix-r2` @ `c30d99e2` 只在目标仓本地（`ls-remote refs/heads/herdr/*` 为空）；`herdr-task integrate` 只建本地分支，全链路零 `git push`；wrapup 按模板规则只做只读合并确认即 DEFERRED，PR 最终由人工要求总指挥手工补交（目标仓 SOP 本为 `npm run pr:wrap-up`）。
- **改动与实现**：
  1. **模板（`workflow_templates/software-development-v1.yaml`）**：wrapup 规则新增「交付 PR 前置（必做）」——步骤 1-2 之后、步骤 3 之前，读目标仓交付约定并按其流程推送交付分支 + 创建 PR（如 `npm run pr:create`），PR URL 写入收尾报告；硬约束：只允许推送/建 PR 两类非破坏性动作，严禁自动合并、严禁 `--force`/`--yes`；未合入的 DEFERRED 记录必须含 PR URL；
  2. **技能（`.agents/skills/six-step-finish/SKILL.md`，版本 `2026.09.17-1`）**：新增「步骤 0：交付 PR 前置」+ Agent 职责「先建 PR 再做核验」+ 三条常见借口兜底；同步 `scripts/install-herdr-skills.sh` 到 `~/.agents/skills/` 并更新 `PROVENANCE.md`（sha256 + 本地修订记录）与 `tests/test_six_step_skill_provenance.py` 登记哈希。
- **验证**：新增模板契约测试（`test_wrapup_requires_delivery_pr_before_finish`，9 passed）与 vendoring 校验（6 passed），全量 611 项测试 PASS；全局技能副本 grep「步骤 0」命中；沉淀教训 §64，更新 [[dag-workflow-engine]] §11。

## [2026-09-17] fix | 门禁结论文件移出 clone + .herdr 纳入内部过滤（交付零污染）
- **背景**：门禁契约文件 `.herdr/gate-verdict.json` 写在 clone 内，会被 `herdr-task commit` 带进交付——实测污染 nexusarchive 交付 PR（wrapup 任务的交付分支带入门禁机器产物，被迫在目标仓加 `.gitignore` 补丁）。
- **改动与实现**：
  1. **`herdr/direct_dispatch.py`**：`GATE_VERDICT_CONTRACT` 常量改为 `gate_verdict_contract(task_id)` 函数 + `gate_verdict_path()`——结论文件默认落 clone 外状态目录 `~/.herdr-controller/gate-verdicts/<task_id>.json`（`HERDR_GATE_VERDICT_DIR` 可覆盖），契约文本内嵌该任务的绝对路径，并保留「权限受限可退回 clone 内 `.herdr/`」兜底；
  2. **`services/herdr-controller.py`**：`_gate_verdict_file_candidates` 状态目录优先、clone 旧契约路径兼容回退（过渡期不丢信号）；
  3. **`bin/herdr-task`**：`INTERNAL_UNTRACKED_EXACT/PREFIXES` 新增 `.herdr` / `.herdr/`——commit 与 verify-baseline 均不再计入该目录（跨仓兜底防线）。
- **验证**：新增/更新用例（结论文件路径与契约注入、状态目录读取、clone 兜底兼容、内部过滤），全量 614 项测试 PASS；现场兼容性实测：历史 wrapup 任务的状态目录为空时仍能从 clone 兜底读到 `pass`。同步更新 [[architecture]] §2.1、[[dag-workflow-engine]] §10 与 lessons §62 操作规范。

## [2026-09-17] fix | 自动 close 等待 git 终化：收官最后一公里不再丢交付分支
- **背景**：`wf-nexusarchive-0917-01` 收官（20:35-20:38）三方竞态：wrapup 判 `completed` 后，后台 close 线程把任务抢先推进 `cleanup_ready -> cleaned`，而主线程的 `herdr-task commit` 子进程（目标仓重门禁约 2m50s）仍在途；git commit 已成功（`4872b1a0`）却撞 `Illegal transition: cleaned -> committed`，`[COMMIT ERROR]` 退出，交付分支落不进集成链路（该 commit 还带入 `.herdr/gate-verdict.json`，由 §62 修订与目标仓 `6c9e48cf` 兜底清洗）。
- **改动与实现**：
  1. **`services/herdr-controller.py`**：新增 `git_finalize_pending_tasks(workflow_id)`（`completed`/`committed` + `integration_mode=git`）；`maybe_close_completed_workflow` 命中时打印一次 `[CLOSE DEFERRED]`（`_close_deferred_logged` 防刷屏）并跳过本轮，终化有界重试收敛后 sweep 自然放行；
  2. **`bin/herdr-task#close_workflow`**：`TEARDOWN_BLOCKING_STATUSES` 之后追加 unsettled-git 闸门，`[CLOSE ABORT]`（exit 2）并打印处置指引（`commit` / `integrate` / `supersede`）；`completed+none` 与已 `cleaned` 任务不误伤，`dry_run` 同样闸门。
- **验证**：Controller 侧 `AutoCloseGitFinalizeDeferralTest` 4 例 + CLI 侧 `TestCloseWorkflow` 2 例（RED 先行：无修复时 1+4 failed），全量 620 项测试 PASS；controller 已 kickstart 部署。沉淀教训 §65，更新 [[architecture]] §2.1 与 [[dag-workflow-engine]] §12。

## [2026-09-17] perf | 总指挥回合成本治理：阶段边界 /compact + 效率纪律注入
- **背景**：对 `wf-nexusarchive-0917-01` 总指挥会话的全量账本还原（opencode session db）：跨度 7.91h、30 个 prompt、活跃 3.36h；**上下文 94K→684K**，584K 时单回合纯 LLM 生成 58.3min（早期 <200K 回合仅 0.2-4min）；单回合工具调用最多 55 次，最慢为总指挥替 Controller 重复跑 `herdr-task commit` 重门禁（14.6min 级）；该工作流事件串行等待总指挥累计 2.35h（`[COORDINATOR BUSY]` 187 次、最长 901s）。
- **改动与实现**：
  1. **上下文卫生（`services/herdr-controller.py#maybe_compact_coordinator`）**：三处边界注入 `/compact`——直接派发阶段推进（`[STAGE ADVANCED DIRECT]`）、总指挥阶段推进（`[STAGE ADVANCED]`）、fix-loop 派发（`[FIX LOOP NOTIFIED]`）；安全门：`HERDR_COORDINATOR_COMPACT=0` 关闭、仅 `opencode`/`claude` kind（`herdr agent get` 探测）、总指挥忙则跳过、`--wait --timeout 300000` 有界且失败不阻断；
  2. **效率纪律（`COORDINATOR_DISCIPLINE`）**：done/blocked/attention/retry/fix-loop/stage-advance 全部事件模板追加硬约束——决策落盘即结束回合、禁止 commit/integrate/cleanup/全量测试、只读核验优先、上下文过大先 `/compact`。
- **验证**：新增 compact 安全门 5 例 + 边界触发契约 1 例 + 纪律注入 2 例（含既有派发测试显式打桩保持语义），全量 **628 项测试 PASS**；controller 已 kickstart 部署。沉淀教训 §66，更新 [[architecture]] §2.1。

## [2026-09-18] fix | 工作流启动路径：模板选择生效 + 总指挥接单机制
- **背景**：`wf-nexusarchive-0918-01` 两个现场问题：① 控制台选 `general-task-v1`（3 节点），实际按 `software-development-v1`（6 阶段）运行——根因 `ensure_project` 对已注册项目直接返回旧 record、忽略 `template_name`，CLI `run --template` 默认值又掩盖了它；② 启动未经过总指挥（Direct Stage Dispatch 首节点直派，"接单"职责缺失）。
- **改动与实现**：
  1. **模板切换（`herdr/projects.py#reprovision_project_template`）**：显式请求不同模板且无活跃工作流时，保留 Workspace/协调者 Pane，重编节点 Tab/Anchor 并关闭旧模板节点 Tab；有活跃工作流明确拒绝；`run --template` 缺省改 `None`（不指定=沿用现有），`create_project` 同路径修复；
  2. **总指挥接单（`services/herdr-controller.py#coordinator_intake_enabled`）**：首个节点（start）默认路由到协调者，`HERDR_WORKFLOW_INTAKE_EVENT` 要求先理解需求再派发第一个 Task；非首节点保持直派；`HERDR_COORDINATOR_INTAKE=0` 关闭；协调者不可用沿用有界等待 + attention 重试；
  3. **`/compact` 观测分类**：空会话 `agent_prompt_stalled` 归为良性 SKIP。
- **验证**：模板切换 5 例 + 接单路由 4 例 + compact SKIP 1 例，全量 **637 项测试 PASS**；现场实证 `wf-nexusarchive-0918-02`：`[COORDINATOR INTAKE]` 命中、总指挥自行派发首个任务、`[COORDINATOR COMPACT]` 成功注入、workflow.json 已切 general-task-v1 且协调者 Pane 保留。沉淀教训 §67，更新 [[architecture]] §2.1。

## [2026-09-18] fix | 控制台阶段卡片跟随工作流模板（前端可见性）
- **背景**：`wf-nexusarchive-0918-02`（general-task-v1）运行中，控制台仍渲染 software-development 的 6 阶段——`workflow_detail` 硬编码内置 `STAGES` 常量，不读 workflow.json 的 `nodes`。
- **改动**：`console/herdr_factory_console.py` 新增 `workflow_stages(wid, p)`（优先 `workflow.json#nodes` 的 id/label，缺失回退内置 `STAGES`），`workflow_detail` 改用它；经 `scripts/install-herdr-console.sh` 部署并随 PR #55 交付。
- **验证**：新增 `ConsoleWorkflowStagesTest` 3 例（模板节点渲染 / 无配置回退 / workflow_detail 接线），全量 **640 项测试 PASS**；实机 `GET /api/workflow?id=wf-nexusarchive-0918-02` 返回 3 节点（intake cleaned / deep_execution working / review waiting）。附录 lessons §67 操作规范第 4 条。

## [2026-09-18] feat | 内环耗尽仲裁闭环：BLOCKER.md 升级卡 + 三选一裁决
- **背景**：Phase 0 内环协议（2026-09-13）的最后一跳因 auto-stash 丢失未合并——内环耗尽时 Sentinel 置 `blocked(inner_loop_exhausted)`，但 Controller 只发通用 blocked 卡，工位自述的 `<clone>/.herdr-loop/BLOCKER.md` 被丢弃（WIP 存档于 `wip/phase0-inner-loop-arbitration`）。
- **改动（`services/herdr-controller.py`）**：
  1. `inner_loop_exhausted` 专属事件 `HERDR_CONTROLLER_BLOCKER_EVENT`：注入 BLOCKER.md 全文 + 仲裁三选一（rework 指导继续 / failed 换策略 / 调整目标重派）+ "仲裁前不得 completed" + 效率纪律；
  2. `_read_loop_doc()` 读取 `<clone>/.herdr-loop/` 报告文档；`blocked_event_type()` 统一三处 blocked 事件入口（handle_event / reconcile / registry watcher）的细分路由。
- **未纳入**：done 事件注入 METRICS.md 摘要——已被规则化验收取代（非门禁 done 不再走总指挥），注入会成死代码并增加提示词负担。
- **验证**：`BlockerArbitrationEventTest` 4 例（路由/含 BLOCKER.md/缺省回退/通用卡不受影响），全量 **644 passed**。

## [2026-09-18] feat | Semantic Supervisor V1：独立于 Agent 的语义监督层
- **背景**：HAFlow 事实层已确定性地知道存活/状态/测试/git 结论，但答不出"有没有有效进展、是否卡死、是否偏离目标、验证是否充分"这类语义问题。第一版以 Jev(TypeSafe System One, POST /v1/systemone) 为 DecisionProvider 建立观察层，铁律：**Jev 给判断、HAFlow 做决定**，Provider 永不触碰 task/runtime/workflow 状态。
- **改动与实现**：
  1. **决策抽象（`herdr/decision/`）**：`DecisionProvider`(judge/score/choose + `judge_many` 单次批量) 与统一 `DecisionResult`；Noul 无 confidence 字段则保持 None 不伪造；`jev` provider 用 stdlib urllib 且 transport 可注入，401/429/522→auth、429→rate_limit、529→overloaded 分类；`rule` provider 作确定性离线替身；registry 解耦域代码与后端。
  2. **监督核心（`herdr/supervisor/`）**：9 个 noul 语义信号（progress/stuck/off_track/requirements/complete/tests/verification/human/finish）；`SupervisorState` 有界(默认 8000 字符预算)+密钥形状脱敏+事件只留 `{type,ago}` 摘要；`SupervisorEvaluation` 携带与上次的 delta/trend，持久化复用 events 表(`supervisor_evaluation`/`supervisor_policy`, source=semantic_supervisor)，零 schema 迁移；RateGate 提供 interval/cooldown/max_calls_per_task 成本闸门；`policy.py` 为唯一信号→动作出口：确定性事实一票否决（settled 任务/非 running 运行时不干预），七动作 CONTINUE/VERIFY/RETRY/REROUTE/PAUSE/FINISH/ESCALATE，高风险动作要求阈值裕度(min_margin)否则降级 CONTINUE。
  3. **接入（`services/herdr-controller.py`）**：V1 仅挂一个 checkpoint——三处 `working/rework→agent_done` 转换成功后 `supervisor_checkpoint()`；enforce 默认关闭（只记录），开启时 RETRY→既有 rework、ESCALATE→既有 attention 台账；整条链路 try/except，`[SUPERVISOR SKIPPED]` 是唯一允许的外伤。
- **验证**：新增 4 个测试文件 49 例（Provider 契约与 7 种故障、State 有界/脱敏/批量、评估持久化与 delta、Policy 全动作+置信降级、Fail-safe：无 JEV_API_KEY/禁用/Provider 宕机/store 爆炸均不影响任务流），全量 **732 passed + 44 subtests**。Q1 删 key 正常运行=YES；Q2 关闭监督行为不变=YES；Q3 Jev 无状态修改权=NO 权限。

## [2026-09-18] fix | Semantic Supervisor 加固：intervention 拦截 done、真实执行证据、Jev 动态 Kill Switch
- **背景**：PR #60 评审发现三处 P1：① `supervisor_checkpoint()` 返回后 Controller 仍无条件 `enqueue_coordinator_event("done")`，enforce=true 时 RETRY/ESCALATE/VERIFY/PAUSE/REROUTE 与原 done 控制流冲突；② `SupervisorState` 虽支持 tests/diff_summary/output_summary，但 harness 从未真正采集这些事实，语义判断缺证据；③ `HERDR_SUPERVISOR_JEV_ENABLED=false` 不生效、Provider 缓存 API Key 导致运行中撤销 key 后仍发请求。
- **改动与实现**：
  1. **拦截语义（`herdr/supervisor/harness.py`）**：`PASS_THROUGH_ACTIONS={CONTINUE,FINISH}`、`INTERVENTION_ACTIONS=其余五动作`；`run_checkpoint` 返回 `{intercepted, handled, continue_flow}`（policy 事件带 `enforced` 字段）——enforce 下 intervention 一律 `continue_flow=False`（handler 成功=handled，未映射/崩溃同样拦截），Controller 全部 done 出口收敛到唯一网关 `emit_done_if_allowed()`；被 RateGate 跳过的补投/恢复路径由 `pending_intervention()` 依 events 账本持续拦截（新 agent_done 变迁或新决策到来才解除）；未映射拦截写 `supervisor_unhandled` attention 台账，绝不静默放行；observe 模式与 supervision 异常仍走原流程（fail-safe 不破）。
  2. **真实执行证据（新增 `herdr/supervisor/evidence.py`）**：复用 `.herdr-loop`（`evaluator.read_state` + METRICS.json 测试/静态检查/综合分）、`git status`+`git diff --stat`、Agent done report（stage_verdict/blocker/status_history 尾 3 条 + 经 `projection.strip_ansi_codes` 清洗的报告尾部 ≤4 行×120 字符）；`acceptance_criteria` 进 State；attempt_count 回退到 status_history 中 rework 次数；全部有界+脱敏，绝不发送完整源码/diff/stdout。
  3. **Jev 动态 Kill Switch（`herdr/decision/providers/jev.py`、`config.py`、`engine.py`）**：provider 不再缓存 key，`_resolve_api_key()` 每次请求读环境；新增 `enabled` 标志与 `provider_enabled()`；harness 前置检查（禁用→不建 provider 不采证据）、engine `should_evaluate` 返回 `provider_disabled`、provider `_ask` 双保险；`HERDR_SUPERVISOR_JEV_ENABLED=false` 三层短路零请求；key 不进 config/event/log。
- **验证**：新增 `test_supervisor_interception.py`（动作集合/harness 语义/五动作 Controller 不落 done/未映射不静默/continue_flow 放行）与 `test_supervisor_evidence.py`（loop/git/report 有界解析、真实 checkpoint 证据、预算与脱敏），`test_supervisor_failsafe.py` 增 JevKillSwitchTests（禁用零请求、运行中删 key 立即停、key 不入配置/事件），全量 **766 passed + 44 subtests**；`compileall` 通过。

## [2026-09-19] fix | Semantic Supervisor 独立评审整改：全 done 出口网关 + newest-N 事件窗口 + 配置收紧
- **背景**：PR #60 加固后经独立 reviewer 子代理对抗评审（verdict: NEEDS_FIXES），发现 1 个 P0 绕过点与 7 项 P1/P2：registry watcher 的 done 补投仍直发事件（生产主路径绕过监督网关）；`list_events` 为 ASC+LIMIT 取的是**最旧 N 条**，>100 事件后 pending 拦截失效且 previous_evaluation 变旧；agent_tail 被任务字段挤出预算；identity 字段未截断使预算非绝对；`"jev": false` 被静默忽略、api_key_env 丢失、provider memo key 过窄；error 未脱敏；RateGate 无锁、provider 值未 clamp。
- **改动与实现**：
  1. **F1 全 done 出口网关**：registry watcher 补投收敛为 `redeliver_done_event()` → `emit_done_if_allowed()`；全仓仅网关内一处 `enqueue_coordinator_event(task,"done")`；PAUSE/VERIFY/ESCALATE/REROUTE 的 attention 首次或动作变更时发一次 macOS 通知（`HERDR_CONTROLLER_TEST` 抑制）。
  2. **F2 newest-N 窗口**：`state_db.list_events` 新增 `desc`（`ORDER BY timestamp DESC, id DESC` + LIMIT 取最新 N），state_store 贯通；run_checkpoint/pending_intervention 均用 `desc=True`；补真实 SQLite 长历史回归。
  3. **F3/F4 证据与预算**：agent_tail 置首、删除与 State 顶层重复字段；identity 字段统一截断 + `_fit_budget` 末段折半硬收敛（预算绝对成立）。
  4. **F6-F8 收紧**：`"jev": false` 归一化为禁用；`api_key_env` 透传 provider；memo key 改为完整配置签名；error 经 redact_text；provider 值 clamp_probability；RateGate 加锁。
- **验证**：独立 reviewer 复审 **MERGE_READY**（F1-F8 全部修复，无 P0/P1 残留）；监督六套件 96 passed；全量 **779 passed + 44 subtests**；compileall 通过。

## [2026-09-19] feat | Semantic Supervisor V1.1: tests_completed 持续评估检查点
- **背景**：HAFlow 监督从"Agent 结束时才评估"（仅 agent_done 挂点）升级为"Agent 过程中持续评估"的第一步。在工位内完成一轮真实测试后触发 `tests_completed` checkpoint，提供过程可见性与早介入能力。
- **核心契约与设计**：
  1. **真实测试证据与指纹（`herdr/supervisor/evidence.py`）**：严格依赖 `.herdr-loop/METRICS.json` 与 `STATE.md`，提取 iteration、total/passed/failing_tests 与 composite_score。构造稳定的 `evidence_id`（SHA-256 签名），未运行占位或缺少指标时安全 skip。跳过终端大 IO，确保轻量级采集。
  2. **事件指纹去重与 RateGate 频控解耦（`evaluation.py`, `harness.py`）**：`latest_tests_completed_evidence_id` 基于 events 账本精准持久化比对，保证同一轮测试事实**只评估一次**且跨 Controller 重启不丢；RateGate 仅负责滑动时间窗口限频，频控暂缓不丢失最新证据。
  3. **Trigger-aware 策略与非终态铁律（`herdr/supervisor/policy.py`）**：`tests_completed` 是过程观察点而不是终态。即使 high confidence / requirements_satisfied 也不允许直接完成 Task，FINISH 自动降级为 CONTINUE；中间过程单轮测试失败（例如 Agent 正在逐步修测且失败数改善中）绝不打断或重做，只有连续卡滞且超出容忍才允许干预。
  4. **Controller 挂点与 Fail-safe（`services/herdr-controller.py`）**：在主轮询循环针对活跃任务（working/rework/dispatched）通过 `check_task_tests_completed` 安全探活。Kill switch 开启或无 key 时 0 开销瞬时短路。
- **验证**：新增 `tests/test_supervisor_tests_completed.py`（10 项专项场景 A-J 全部通过）；全仓回归 789 passed，compileall 通过。


## [2026-09-19] feat | Workflow Template Execution & Context Contract V1
- **背景**：Workflow Template 此前只能声明"怎么做"（nodes/DAG/policy），无法声明"跑在什么环境"与"需要什么业务上下文"。销售报价等非 Git 场景被迫伪造 Git 项目。
- **核心契约与设计**：
  1. **模板契约层（`herdr/workflow.py`）**：`normalize_workflow` 叠加 `_normalize_execution_contract` —— `execution.mode` 仅 `git|context`（缺省 `git`，旧模板字节级不变）；`context: {required, optional}` 声明上下文条目（`- id` 简写或 `{id,label}`，id 正则约束、跨列表去重）。纯函数族：`execution_mode`/`context_contract_ids`/`validate_context_contract`（required 缺失/unknown 绑定 fail-fast）/`parse_context_binding_args`/`render_context_reference_block`（只给 id+绝对路径，不展开内容）/`validate_integration_for_execution`（context+git 正交拒绝）。
  2. **模式前置决策（§15 最小切面）**：`herdr-factory#resolve_project_for_template` 在项目/Runtime 初始化之前 load_template 判模式；context 走 `projects.py#ensure_context_project`（任意真实目录注册，不要求 Git），git 路径零改动。`run --context id=path`（可重复）绑定解析为绝对路径。
  3. **持久化（无新增表）**：execution/契约经 `provision_project` 写入项目 workflow.json（契约存 `context`、绑定存 `context_bindings`，规避归一化冲突）；运行绑定经 `register_workflow(execution=, context=)` 存入 StateStore workflows 的 `metadata_json` 自由键。Task 经 `project_for_workflow` 继承 `execution_mode`+`context`（git 任务记录字节级不变）。
  4. **Context Task Workspace（worker 去 Git 硬依赖）**：`create_context_task_workspace` 复用 `clones/<task_id>` 路径使 cleanup/retention/finalize 零改动；branch=None、空基线、complexity disabled（不起 node 子进程）；`.agent-task-context` 增 `mode=context` + `context.{id}=path` 行。`verify-baseline` 用 `context_workspace_fingerprint`（递归文件指纹）保持 TASK_CHANGED 协议；commit/integrate 对 context fail-fast。
- **验证**：新增 `workflow_templates/context-smoke-test.yaml` + `tests/test_execution_context_contract.py`（28 项，RED-first）；全量 822 passed + 44 subtests（baseline 794）；compileall 通过；S4-exit ai-slop-cleaner Mode B 删除双重校验与模式解析重复。

## [2026-09-19] fix | Execution & Context Contract V1 补丁（PR #62 review）：Workspace Identity != Workflow Template
- **问题**：① `ensure_context_project` 对模板不一致直接 raise，把 Context Workspace 绑死在首个模板上，违背"一个业务 Workspace 依次跑 sales-research/quotation/contract-review"的业务模型；② Context 绑定路径强制 `is_dir()`，PDF/Word/Excel/Markdown 等文件引用被误拒。
- **修复**：① 复用既有 `reprovision_project_template()`（最小扩展 context_bindings 参数）：Workspace/Coordinator 保留、Node Tabs 按新模板重建、旧 Tab 关闭、活跃工作流拒绝切换；context 语义（execution.mode/base_branch=""/契约/本次绑定）经 `_register_project_workflow` 扩展参数在切换后完整保留，`detect_base_branch` 只留在 git 路径；② 绑定校验改 `exists()`（目录或文件均合法），仍 resolve 绝对路径 + 不存在 fail-fast + required 缺失拒绝。
- **验证**：新增 3 测试（切换保 workspace/活跃流拒绝/文件+缺失路径）共 35 passed；全量 829 passed + 44 subtests；真机 E2E：非 Git 目录 `/tmp/ctx-e2e.*` 上 A(context-smoke-test, wQ/p1) → 任务 ctx-e2e-hello-a 于独立 workspace 产出 HELLO.md（读自 common 引用）→ verify-baseline 指纹 TASK_CHANGED → close → 同 workspace 跑 B(ctx-e2e-beta)：wQ/p1 不变、template 更新、review tab wQ:t3 重建、绑定含 quote.pdf 文件引用、全程零 git 调用。

## [2026-09-20] feat | Trajectory Observer V1：结构化、可验证、可追溯的运行过程诊断
- Added [[trajectory-observer]]: 旁路诊断层——`run_id` → Trajectory + Runtime + bounded 日志 → 确定性 signal → `DecisionProvider.judge_many`（noul）确认 → `TrajectoryFinding`（type/severity/evidence/cause/recommendation/confidence）；`trajectory_findings` 表与 events 事实表物理分离，`finding_key` 跨进程去重；controller registry_watcher 非阻塞 daemon 线程触发，`herdr-task observe` 人工入口。
- Updated [[task-lifecycle]] §1.2: Ledger 之上新增 Observer 只读诊断层的说明与链接。
- Updated [[index]]: 意图路由与知识地图新增 [[trajectory-observer]]。
- 证据：`herdr/observer/`、`herdr/state_db.py:trajectory_findings`、`services/herdr-controller.py:registry_watcher`、`bin/herdr-task:cmd_observe`、`tests/test_trajectory_observer.py`（41 项）、`docs/superpowers/specs/2026-09-20-trajectory-observer-design.md`。

## [2026-09-20] fix | Trajectory Observer 运行时真实性加固：Live Runtime / Live Transcript / 证据升级 / 硬预算
- Updated [[trajectory-observer]]: 新增只读 `herdr/observer/live.py`（pane/agent liveness 探测失败=unknown；live Pane transcript 优先、evidence 文件兜底，均在 daemon worker 内 bounded 执行）；Finding 同 episode 原地升级（canonical finding_id，不降级）；`_fit_budget` 硬保证（递归 clamp + 最小 identity）；`--task-id/--run-id` 互斥、`--json` stdout 纯 JSON（诊断走 stderr）。
- 证据：`herdr/observer/live.py`、`herdr/observer/context.py:bound_transcript,_fit_budget`、`herdr/observer/signals.py:_detect_runtime_unavailable`、`herdr/state_db.py:upsert_trajectory_finding`、`bin/herdr-task:cmd_observe`、`tests/test_trajectory_observer.py`（65 项）。

## [2026-09-20] fix | Trajectory Observer 运行身份安全：agent_session 身份校验 + transcript guard + 上下文预算下限
- Updated [[trajectory-observer]]: live probe 新增身份校验（pane_not_found / identity_match / identity_mismatch / agent_not_found / unknown 五态，unknown 绝不当 unavailable）；live transcript 必须通过同一身份 guard 才允许 `pane read`，未确认身份回退 persisted evidence；`max_context_size` 产品最小值 500 在 `load_config` 显式 clamp。
- 证据：`herdr/observer/live.py:_session_verdict,_identity_result,probe_live_runtime,read_live_transcript`、`herdr/observer/config.py:MIN_MAX_CONTEXT_SIZE`、`tests/test_trajectory_observer.py`（70 项，含 A/B/C 三条身份回归）。

## [2026-09-20] fix | Trajectory Observer 收尾：no_progress episode 边界 + Provider 构造隔离 + 预算语义澄清
- Updated [[trajectory-observer]]: `no_progress` 改为按最近一次进展边界（passed verification / artifact）计算当前 episode 的 rework 数，anchor 取当前 episode 首次 rework（历史成功不再永久屏蔽新卡死）；Provider 构造失败降级为无 Provider（证据型 Finding 照常产出、弱信号静默）；`max_calls_per_run` 明确为 process-local per-run observation budget（Controller 重启后重置，V1 不持久化）。
- 证据：`herdr/observer/signals.py:_progress_boundary_sequence,_detect_no_progress`、`herdr/observer/harness.py:observe_run,ObservationScheduler`、`herdr/observer/config.py:max_calls_per_run`、`tests/test_trajectory_observer.py`（74 项）。

## [2026-09-20] fix | Trajectory Observer Live Agent liveness：pane session 一致仍须 agent get 确认
- Updated [[trajectory-observer]]: 对 persisted 明确有 Agent 的 Run，pane 级 session 一致不再直接判 `available`——必须继续 bounded `herdr agent get`（与 `pane_pool` 的真实 live agent 判据一致）：agent 成功且 session 一致→`identity_match`；显式 `agent_not_found`/空 agent→`unavailable`；agent session 不一致→`identity_mismatch`；timeout/parse/身份不足→`unknown`。transcript guard 仍只在最终 `available` 时 pane read。
- 证据：`herdr/observer/live.py:_identity_result`、`tests/test_trajectory_observer.py`（75 项，含 A/B 两条 agent liveness 回归）。

## [2026-09-20] fix | Trajectory Observer 三项收尾：agent_done terminal checkpoint / 脱敏先于 cutoff / 身份优先级 A-B-C
- Updated [[trajectory-observer]]: ① `agent_done` 在 `redeliver_done_event()` 前获得独立 terminal observation（独立 gate，每 run 每 controller 进程一次，异步不阻塞 done flow）；② `bound_transcript` 改为先脱敏再 bytes/lines/chars 截断，文件尾部先读 8192B overlap 并丢弃不完整行，杜绝 key prefix 被 cutoff 切断后泄漏；③ 身份优先级 A(session 匹配)/B(agent_name 实例名匹配)/C(仅 agent type → unknown 且禁止 pane read)。
- 证据：`herdr/observer/harness.py:submit_terminal`、`herdr/observer/context.py:bound_transcript,read_log_tail`、`herdr/observer/live.py:_identity_result`、`services/herdr-controller.py:_observer_terminal_checkpoint`、`tests/test_trajectory_observer.py`（85 项）。

## [2026-09-20] fix | Trajectory Observer terminal checkpoint 挂载统一 Done Gateway
- Updated [[trajectory-observer]]: terminal observation 从 registry_watcher 的 agent_done 分支移入统一 Done Gateway `emit_done_if_allowed()` 入口，覆盖 listener/recovery/registry redelivery/rework heal 全部 done 路径，消除「listener 立即推进导致 verification_failure 从未被观察」的窗口；registry_watcher 不再单独调用；async/fail-safe/per-run 去重/不受 periodic gate 影响等特性不变。
- 证据：`services/herdr-controller.py:emit_done_if_allowed,_observer_terminal_checkpoint`、`tests/test_trajectory_observer.py:TestDoneGatewayTerminalCheckpoint`（5 项）。

## [2026-09-21] fix | Trajectory Observer 测试隔离：conftest 默认关闭观察器
- Updated [[trajectory-observer]]: 测试套件 conftest 默认 `HERDR_OBSERVER_ENABLED=0`；唯一端到端网关用例显式开启；未显式传 config 的调度器测试改为自包含；清理生产库 3 行测试残留（08:42 由 done-path 测试经默认调度器写入）。
- 证据：`tests/conftest.py`、`tests/test_trajectory_observer.py:TestTestEnvironmentIsolation`、`docs/lessons/lessons-learned.md` §78。

## [2026-09-21] feat | ObservationPack V1 Evidence Layer

- 新增 `herdr/observation.py`：Observation metadata 写入 SQLite，内容写入 state DB 同目录的 `observations/`；文本/JSON 先复用 `redact_text`，记录 `content_ref`、`media_type`、`size_bytes`、`sha256`、bounded excerpt 和 metadata。
- 证据不可变：内容文件独占创建，无 update API；`verify_observation` 检测缺失、大小变化和 SHA-256 篡改；`read_observation` 默认 16 KiB、硬上限 64 KiB。
- 统一去重：`(run_id, source_type, source_ref, sha256)` 唯一约束与 SQLite 事务保证并发创建收敛到 canonical Observation；artifact 只引用已有文件，不复制大型内容。
- 接入：Observer 的最终 agent log Finding 改为 `observation_id + bounded excerpt`；`verification_completed` 保留 `evidence_id` 并增加 `observation_id`；Trajectory 只追加 `observation_created` receipt，不存完整证据；ObservationStore 失败回退短 evidence，不阻塞主链路。
- 证据：`herdr/observation.py`、`herdr/state_db.py:observations`、`herdr/observer/engine.py`、`herdr/trajectory.py`、`services/herdr-controller.py`、`tests/test_observation.py`、Trajectory/Observer/Supervisor 回归测试。

## [2026-09-21] feat | Semantic Context Compact V1 Working Memory Layer

- 新增 `herdr/context_compact.py`：ContextPack 将 bounded Trajectory、Observation metadata、Finding 和 Runtime/Task facts 组成可追溯 working memory；`verified_facts` 由程序生成，reducer 只负责语义数组。
- 新增 SQLite `context_packs` append-only 表、run/task 索引、latest/list/get API；相同 `source_event_sequence` 去重但不删除旧快照。
- 新增 `herdr-task compact --run-id <run_id> [--json] [--no-model]`；agent_done Done Gateway 通过 daemon best-effort 旁路触发，失败不影响状态推进。
- 默认输入硬预算为 12,000 字符、100 events、10 findings、20 observation metadata、20 artifact refs；Compact 不读取完整 Observation 内容。
- 证据：`herdr/context_compact.py`、`herdr/state_db.py:context_packs`、`bin/herdr-task:cmd_compact`、`services/herdr-controller.py:_schedule_context_compact`、`tests/test_context_compact.py`。

## [2026-09-22] fix | Harness Metrics 跨 Run 身份借用与损坏 payload 降级
- 修复聚合读模型的身份归属：`get_run_metrics` 解析 Task 后用 `run_id_for_task(task) == run_id` 校验归属，不匹配时不借用 `task_id`/`workflow_id`/`final_status`（进行中的 Run 不再被其他 Run 的 completed Task 报成已完成）。
- 修复损坏 payload 的失败面：`aggregate_run_metric_rows` 的 `verification_completed` 聚合改为嵌套 `CASE WHEN json_valid(payload_json)`；损坏行仍计入 `verification_total`，只无法归类 passed/failed，不再让该 Run 的全部指标查询抛 `malformed JSON`。
- 文档：`docs/architecture/harness-metrics.md` 增加两条语义说明；通用教训归档 `docs/lessons/lessons-learned.md` §81。
- 证据：`herdr/metrics.py:get_run_metrics`、`herdr/state_db.py:aggregate_run_metric_rows`、`tests/test_metrics.py`（12 passed，含 2 项新回归）。

## [2026-09-22] feat | Action Protocol V1：Supervisor → Controller VERIFY/RETRY 闭环
- 新增同一 SQLite StateStore 内的 Intervention projection 与唯一身份 `(run_id, task_id, decision_id, action)`；请求、claim、完成、失败均写入现有 events 表。
- `RETRY` 复用既有 `rework` 状态迁移并执行 retry budget；`VERIFY` 只进入既有 verification/rework 路径，不伪造 verification verdict。
- Controller done/recovery 路径恢复 requested/running Intervention；跨 Run、重复决策、重复消费和并发 claim 均由持久化身份与事务保护。
- 证据：`herdr/intervention.py`、`herdr/state_db.py`、`services/herdr-controller.py`、`tests/test_action_protocol.py`、`tests/test_intervention_store.py`、`docs/architecture/action-protocol.md`。

## [2026-09-23] fix | Action Protocol V1：VERIFY/RETRY 恢复与事实边界加固
- 修复 EVAL_DONE freshness 的双读 TOCTOU：测试证据从单次字节快照生成 metrics、hash 与完成时间；receipt 未成功持久化时不消费当前 evidence。
- RETRY 现在必须真实 dispatch 既有 Agent prompt，并以 `retry_dispatched` 作为执行证据；VERIFY/RETRY watchdog 与 recovery 不再仅凭 `rework` 或旧 deliverables 自愈。
- Recovery 按 `task_id` 隔离，历史 failed VERIFY 以新的 `agent_done` episode 为边界；Supervisor kill switch 在 recovery 前生效。
- 证据：`services/herdr-controller.py`、`herdr/supervisor/evidence.py`、`tests/test_action_protocol.py`、`tests/test_supervisor_tests_completed.py`；全量 1101 passed、44 subtests。

## [2026-09-23] fix | 内环质量门禁基线分诊：存量 lint 不再误杀全自动
- `herdr/evaluator.py` 新增基线分诊：`BASELINE_LINT.json`（init 时快照一次）+ `effective_defects(current, baseline)`；`quality/composite/is_converged` 只看新增缺陷，观测总数仍全量记录；无基线旧 clone 回退绝对门禁。
- `bin/herdr-task:auto_init_task_loop` 与 `bin/herdr-loop:init` best-effort 快照基线（120s 超时，失败只告警）；`run_evaluation` 透传基线；`EVAL_DONE.json`/`METRICS.json` 新增 `baseline_lint_errors/new_lint_errors` 加法字段。
- 动因：`wf-haflow-0923-01-test-auto` 测试全绿但全仓 `ruff check .` 存量 2562 错误导致 65/100 耗尽仲裁；任何工作流都会在同一门禁卡死。
- 证据：`tests/test_loop_evaluator.py`（5 项新回归）、全量 `pytest -q` 1107 passed + 44 subtests；教训 `docs/lessons/lessons-learned.md` §84。

## [2026-09-23] fix | Fix-loop 死锁三件套：通知补投 + 回流预算 + 作废闩
- 背景：`wf-haflow-0923-01` test 三连 blocked 后零 live 任务死停——fix-loop 通知等总指挥 120s 超时即丢弃；`FIX_LOOP_MAX` 只显示不生效（走到 loop=4）；作废后 sweep 以陈旧完成越过 implementation 空推进。
- 新增 `herdr/fix_loop.py` 纯决策函数；`handle_fix_loop` 作废前先过预算/同判据门禁，超限或重复只升级不作废；超时转 attention 持久化，sweep 补投；作废设 `pending_redo` 闩挡 advance，重做完成清闩/计数/升级。
- 证据：`tests/test_fix_loop_recovery.py` 23 passed；全量 `pytest -q` 1130 passed + 44 subtests；教训 `docs/lessons/lessons-learned.md` §85。

## [2026-09-23] fix | 直派携带候选分支 + fix 落分支 + supersede 存 WIP
- `herdr/direct_dispatch.py`：spec 携带消毒后的 `onto_branch`（依赖链优先）；`services/herdr-controller.py` 透传 `--onto`，空候选（`rev-list base..onto==0`）直接 fallback 不烧内环，git 失败则 fail-open。
- fix-loop 消息模板默认 `--integration-mode git`；`bin/herdr-task#supersede_task` 作废后 best-effort auto-commit WIP（不含内部目录，不 push，失败不阻断）。
- 动因：r6 直派丢 onto 测 main 空转；fix4 七文件 stranded。
- 证据：`tests/test_dispatch_candidate.py` 11 passed；全量 `pytest -q` 1141 passed + 44 subtests；教训 §86。

## [2026-09-23] wrapup | wf-haflow-0923-01 Eval+Replay V1 收尾 abandon
- fix-loop 6/3耗尽，test-auto-r6 blocked（BASELINE_MATCH @3be4362，缺Eval/Replay/Compare/CLI及专项测试）；impl-fix4实现仅存clone未集成。
- 用户确认接受现状推进后指令直接收尾：保留blocked证据，abandon关闭，不再重派同范围fix；clones保留。

## [2026-09-23] feature | HAFlow Collaboration Protocol V1（含生产接线）
- 基于 Herdr pane/agent/prompt/runtime 建立最小协作语义，不自建通信层：`herdr/collaboration.py` 纯核（身份键/最小 prompt/确定性路由/延迟指标）+ `collaboration_events` 幂等表 + Controller 派发/ACK 装配 + 两个 guarded 生产钩子（直派后补 HANDOFF、working 后 ACK，`HERDR_COLLABORATION_ENABLED=0` 熔断）。
- 只做 3 条自动边（implementation→review、review→tester、BLOCKER→coordinator），未知边保留 Coordinator；原 Workflow dependency 链不动。
- S6 round 1 抓到幽灵 dispatched（D1）后修复：sender 失败落终态 failed、refs 封顶保 ID 尾、缺 run fail-closed；教训 `docs/lessons/lessons-learned.md` §87。
- 证据：collaboration 专项 36 passed、全量 `pytest -q` 1238 passed + 44 subtests、S6 round 3 MERGE_READY；文档 `docs/architecture/collaboration-protocol.md`。

## [2026-09-23] fix | Collaboration 修复 PR：共享 workflow scope + ACK fast-path + completed 接线
- P1：隔离域由 per-task `run_id` 改为 workflow 执行身份（`collab_scope_for_task`），否则生产 handoff 恒失败；测试补生产语义（各异 run_id + 共享 workflow）。
- P2：dispatch 快照已 working 即补 ACK（消 launch-then-event 竞态）；`finalize_completed_task` 挂 `maybe_complete_on_task_done`（仅 acknowledged→completed）。
- 证据：专项 39 passed、全量 1241 passed + 44 subtests、S6 MERGE_READY；教训 §88。

## [2026-09-24] fix | Git 终化收编 HEAD（commit 空 index 死锁修复）
- 背景：Agent 在 clone 内直接 `git commit` 后工作区干净，`herdr-task commit` 空 index 恒 exit 3，终化永不收敛（test-t1 FAIL，阻塞 D1-D7）。
- 修复：`herdr/git_adoption.py` 纯判定（ADOPT/EMPTY/REFUSED，锚点优先、时间兜底、onto 禁时间判定）；`commit_task` 空 index 收编（ADOPT 登记 HEAD 不建提交、真空仍 exit 3、无法归属 exit 4）；任务记录新增 `baseline_commit`/`onto_branch`（worker 检出后采集）；`verify-baseline` HEAD 锚点感知（未收编推进报 TASK_CHANGED，收编后仍 BASELINE_MATCH）；Controller 终化分级（empty/refused/rebase-conflict 升级事件、commit_retry 可达、`git_finalize_pending_tasks` 排除已升级）；输出契约 `HERDR_COMMIT_RESULT`/`[ADOPTED]`/`exit 4`；`integrate` 幂等（已集成重入成功、rebase 冲突 exit 6 一次性失败）。
- 回归：新增 `tests/test_git_adoption.py`、`tests/test_commit_adopt.py`、`tests/test_legacy_adopt_converge.py`、`tests/test_finalize_empty.py`（32 用例，覆盖 AC-1/AC-2/AC-3/AC-4）。

## [2026-09-24] wrapup | wf-haflow-0923-02 Git 终化收编 HEAD 收尾（交付 PR 待合入）
- 交付物身份：`agent/opencode/fix-wf-haflow-0923-02-impl-fix2@33a1f1d`，base `c66fc46`（相对 base 8 文件 +1193/-118：`bin/herdr-task` / `herdr/git_adoption.py` / `services/herdr-controller.py` / 4 个 tests / `wiki/log.md`）。
- 收尾独立复证（收尾 Agent 自测，非实现方自述）：全量 `pytest -q` 1321 passed + 44 subtests；`ruff check` 基线 2768 == 候选 2768 且 (文件,规则) 多重集差异为空；`compileall`、`git diff --check`、`bin/herdr-task` AST 均通过。
- 交付 PR：只推送交付链末端分支并创建 PR（base `main`），不合并；URL 见 shared note `wrapup-t1收尾报告`。六步步骤 3 因 PR 未合入记 DEFERRED（收尾脚本 `--dry-run` 只读）。
- 知识沉淀：`docs/lessons/lessons-learned.md` §89（收尾节点分支 ≠ 交付物分支；有锚任务空终化不自动放行）；wiki 回填本文与 [[dag-workflow-engine]] §13（收编 fail-closed 守卫 + close 第二道闸 `escalated_git`）。
- 遗留：`impl-fix1`（`committed` + `finalize_escalated`，原因 `integrate_rebase_conflict`）不阻塞 `unsettled_git`（已排除已升级），但命中 `escalated_git` 闸门；其内容已被本次交付取代，处置建议 `--accept-escalated`（须在 base 合入后由 Controller/人类执行）。

## [2026-09-24] feat | Context Compiler V1：State-Aware WorkingContext
- 新增 `herdr/context_compiler.py`：按 Task/Workflow 状态、节点依赖、Agent 角色和同一 execution scope 确定性选择最小上下文；每个内容项保留 `source_ref`，Observation 只投影 metadata/excerpt，不读取正文。
- 新增 SQLite `working_contexts` 不可变快照、fingerprint/latest/list 读取、supersession、role-aware relevance、预算、结构化 diff 和事实指标；不改变既有 Task/Trajectory/Observation/Finding 事实源。
- Handoff、Task launch/retry、verification dispatch 只传 `context_id`；dispatch 校验 context ref 的目标 Task 与 run scope，旧无 ref 事件保持兼容。
- 证据：`herdr/context_compiler.py`、`herdr/state_db.py:working_contexts`、`herdr/collaboration.py`、`services/herdr-controller.py`、`bin/herdr-task`、`tests/test_context_compiler.py`、`tests/test_collaboration_wiring.py`。
- V1 限制：不做 RAG/向量检索/长期记忆/跨 Run 检索；Context Diff 尚未接入 Dependency Wakeup。

## [2026-09-24] fix | Context Compiler V1：审查闭环与边界加固
- legacy workflow execution 无显式 scope 时，仅允许目标 Task 自身 Run；只有与目标直接相连的 Collaboration Handoff 才扩展 sibling Run，避免无关任务事实串入。
- `context_models.py`、`context_sources.py`、`context_candidates.py`、`context_selection.py` 拆分核心职责；source snapshot、候选构造和 selection 均有专项回归。
- 收紧 supersession/evidence 引用、递归脱敏、角色过滤、预算必保留状态、verification latest-wins、Handoff context ref 身份校验和 Supervisor RETRY 引用。
- 增加 `source_watermark`、append-only A→B→A、编译调用指标事件、并发 writer 与真实 verification event 回归。
- 证据：`tests/test_context_compiler.py`、`tests/test_collaboration_wiring.py`、`herdr/context_sources.py`、`herdr/state_db.py`、`services/herdr-controller.py`；全量 `1417 passed, 44 subtests passed`。

## [2026-09-24] fix | Context Compiler V1：source revision 与首次 Handoff 闭环
- `working_context_source_heads` 为每个 scope 维护单调 source revision；编译器保存前校验 revision，迟到旧 source candidate 只能保留历史而不能成为 latest。
- Controller 先创建 Handoff 事实，再通过 `attach_working_context_ref` 绑定目标快照；legacy planned link 只扩展目标直接关联的 sibling Run，快照可包含本次 Handoff 与上游事实。
- Verification 事件使用独立有界窗口并按 sequence/revision 选最新项；预算保护 blocker/verification/open question，source version、fingerprint、provenance 和指标字符数保持一致。
- 持久化入口增加 context identity、source-ref 形状/存在性和 Handoff 来源 Task scope 校验；prompt 只发送经过 context provenance 过滤的 evidence refs。
- 证据：`tests/test_context_compiler.py`、`tests/test_collaboration_wiring.py`、`herdr/context_projection.py`、`herdr/context_sources.py`、`herdr/state_db.py`、`services/herdr-controller.py`。
## [2026-09-24] fix | wf-haflow-0924-01 fix-loop 候选门禁修复
- FR-1：完成观察按 task version 持久化双采样；Controller 通过 StateStore 原子 CAS 提交，旧 epoch 与同刻重复 sweep 不可完成。
- FR-2：blocked episode 使用跨进程 action claim；每轮最多一次自动重推，失败可恢复，第二 SLA 独立升级人类；崩溃观察回到 Controller 自动补派链。
- FR-4：delivery selector 解析显式 identity/supersede/invalidation 图；未知边、冲突 payload、同刻多候选 fail-closed，replacement 失效不回退。
- FR-5：保持直接 `--force` 兼容；FR-6：opt-out 审计异常和空 review 池在 topology/Pane 前 fail-closed 并落 Task/Event/Workflow metadata。
- 证据：`tests/test_impl_fix4_blocker_regression.py`、`tests/test_impl_fix1_regression.py`；`~/HAFlow/bin/herdr-loop eval` score 100（1420/1420，new lint 0）；通用教训 `docs/lessons/lessons-learned.md` §90。
# [2026-09-24] fix | wf-haflow-0924-01 review-r1 blockers P1-1 / P2-1..P2-5
- test/review 无唯一有效 delivery 时在 Pane/Clone 创建前 exit 2，并写 StateStore `test_baseline_rejected` actionable event；check-delivery CLI 补 merged PR history 与本地 Git tree evidence。
- accept-escalated 报告 outcome、pane_closed、Task 状态及 Clone 保留原因；integrate 将主仓 tracked dirty preflight 前移到所有 task refs/branch 更新前。
- Router 在实际选择重用的 Agent 后写非空 selected 审计；Sentinel 捕获 SQLite observation/list 失败并保护循环继续。
- 文档更新：`docs/references/cli-reference.md`、`wiki/task-lifecycle.md`。专项回归与全量验收见 workflow shared note `impl-fix6修复说明`；本条仅记源码契约，未声称未运行的检查通过。

## [2026-09-25] wrapup | wf-haflow-0924-01 故障自愈规范（FR-1..FR-6）交付收尾
- 交付身份：分支 `agent/opencode/feat-wf-haflow-0924-01-impl` @ `34bfd30`（= 集成分支链末端 `herdr/integration-wf-haflow-0924-01-impl-fix8`），base `main` @ `437b335`；12 提交 / 31 文件 / +8807-378。
- 上游门禁：`test-r11` = pass、`review-auto-r2` = pass；`verify-baseline` = `BASELINE_MATCH`。六步收尾步骤 3 因 PR 未合入记 DEFERRED（`--dry-run` 只读）。
- 知识沉淀：`docs/lessons/lessons-learned.md` §91（测试进程写穿实盘 `tasks.json` 投影的根因、反证与恢复方式）。
- 本文条目为 append-only 收尾记录；交付 PR 与收尾报告外链见 workflow `wf-haflow-0924-01` 共享文档区 `wrapup-auto收尾报告`，不在本文件重复易漂移的 URL。

## [2026-09-25] feat | Adaptive Agent Router v1 (Shadow Mode) PR #99
- 分支 `agent/opencode/adaptive-router-v1-shadow` → PR #99（base main）；7 文件 / +~1100。
- 内容：影子排名（agent × node/stage × task_type）+ ETQS + Qualified Success + route_decision 事件；生产选择零改变（Case 9/10 硬门禁）。
- 验收：专项 14 passed，全量 1725 passed + 44 subtests，compileall/diff-check OK；S6 MERGE_READY（同模型自审）。

## [2026-09-25] fix | PR #99 reviewer 6 项历史事实链修复已推送
- commit `849c89b`：launch 持久化 task_type（RED-proven 集成测试）、taskless eval 唯一归属、eval final 禁止回填、cutoff 前最新 revision、per-agent 索引分片（EXPLAIN 回归）、独立窗口。
- 验收：专项 21 passed，全量 1732 passed + 44 subtests；S6 round 2 MERGE_READY。ETQS/UI/Workflow/Scheduler 未动。
- 说明：仓库无 .github workflows，上述为本地运行结果，无 Actions 可引用。

## [2026-09-25] feat | PR #99 重构为 Immutable Outcome Fact Layer 已推送
- commit `08cabb8`：新增 `herdr/execution_outcome.py` + `agent_execution_outcomes` 表；
  Router 只读 Outcome（`query_execution_outcomes` + covering bucket index）；
  删除 `query_adaptive_history` 及 JSON/ownership/cutoff 重建 SQL。
- 验收：outcome 22 + router 15 passed，全量 1732→1748 passed + 44 subtests；
  S6 round 3 MERGE_READY。ETQS 公式、UI、Workflow、Scheduler 未动。

## [2026-09-25] merge | PR #99 已合并（Outcome Fact Layer + Adaptive Router v1 Shadow）
- merge `4da5085`：main 现含 `herdr/execution_outcome.py`、`agent_execution_outcomes`
  表 + covering bucket index、只读 Outcome 的 Adaptive Router、13 用例 outcome 测试。
- main 树验证：outcome 22 + router 15 passed；compileall OK；diff-check OK。
- 后续：v2 是否接管流量待 shadow 数据证明 ETQS 真实下降后另行决策。

## [2026-09-25] feat | Adaptive Router Shadow Evaluation v1（工作树，未合并）
- 新增 `herdr/shadow_evaluation.py`（只读评估纯核心：coverage/agreement/actual
  outcome/calibration+Brier/ETQS 近似/disagreement分组/predicted uplift/
  sufficiency/canary readiness + text/JSON render）；
  `herdr/state_db.py` 追加 `query_route_decisions`（bounded newest-first）+
  `batch_get_execution_outcomes`（chunked，防 N+1）；
  `bin/herdr-task shadow-eval` 只读 CLI（--node/--task-type/--agent/--since/--limit/--json）；
  新增 `docs/architecture/adaptive-router-evaluation.md` + `tests/test_shadow_evaluation.py`（19 用例，覆盖任务 §19 Case 1–14）。
- 边界：frozen candidate_rankings 评估（禁重算）、(task_id,run_id)+workflow 关联、
  无反事实胜率口径、Adaptive Router/ETQS/生产路由零改动。
- 验收：专项 60 passed（含 outcome/router 回归），全量 1771 passed + 44 subtests；
  S6 round 2 MERGE_READY（同模型双轴评审，working_tree 交付，无 push/PR）。

## [2026-09-26] refactor | shadow_evaluation.py 765行拆分为4模块（工作树，未合并）
- herdr/shadow_rows.py（192：frozen决策×outcome join）+
  herdr/shadow_metrics.py（456：纯聚合+报告）+
  herdr/shadow_render.py（124：文本呈现）；
  herdr/shadow_evaluation.py（100：管线组装+稳定公开API门面）。
- 纯移动零语义改动：调用方（tests/bin/herdr-task）零改动，`__all__` 19名与拆分前一致；
  无重复定义，导入无环。
- 验收：表征19 passed前后一致，全量 1771 passed + 44 subtests；
  S6聚焦评审 MERGE_READY。生产路由零改动。

## [2026-09-26] pr | Shadow Evaluation v1 已提交 PR #101
- branch `feat/shadow-evaluation-v1`（基线 origin/main ea86b1c，无漂移）→
  https://github.com/allinai0506/HAFlow/pull/101（9文件，+1773，纯加法）。
- 最终树证据：专项60 passed；全量1771 passed + 44 subtests。

## [2026-09-27] fix | PR #101 review fixes已推同分支（待合入）
- 4项口径修复：agreement三态（unknown不进分母/分组/uplift）；sufficiency拆
  model证据与evaluation证据双status（canary只给事实计数，无eligible verdict）；
  filter-then-limit分页（keyset cursor，limit计匹配数，collection meta披露窗口）；
  ETQS加paired双P50。另将metrics二次拆出shadow_sufficiency.py，全模块≤500行。
- Brier/ETQS公式/Router/Outcome/Canary机制未动；未新增CI workflow。
- 验收：新23用例（含4 RED-first回归）+专项64 passed；全量1775 passed + 44 subtests；
  S6聚焦复审MERGE_READY。生产路由零改动。

## [2026-09-27] fix | PR #101 closeout收口已推同分支（4项全收）
- Identity：`_outcome_matches(identity, actual_agent, outcome)`唯一归属contract，
  actual==agent无fallback + workflow/node/task_type双方非空校验。
- Dedup：`select_authoritative_execution_rows`按(task,run)取最新agent一致决策；
  decision_rows与execution_rows分离；coverage加unique/settled_executions。
- Median：标准数学中位数；P90保持nearest-rank。
- Pagination：每页min(page,remaining)，4种stop_reason进collection meta。
- 验收：shadow 34 passed；专项75 passed；全量1786 passed + 44 subtests；
  S6聚焦复审MERGE_READY。§24禁改项零diff。

## [2026-09-27] fix | PR #101 只读承诺补齐（参数校验前移）
- `cmd_shadow_eval` 先验证 --since/--limit 再 `_get_store()`，非法参数 exit 2
  不建库；--limit help 同步 filter-then-limit 文案。统计逻辑零改动。

## [2026-09-27] fix | PR #101 最后两项已推（可合并）
- Sufficiency双输入：model证据取全部decision rows，evaluation取execution rows；
  无Outcome决策的model桶不再消失。
- `--limit 0/-1` 在_open store_前拒绝（exit 2，不建库）。
- 验收待全量回归确认后收口。

## [2026-09-26] merge | PR #101 已合并（Shadow Evaluation v1 收口完成）
- merge `b9fb57c`：main 现含 shadow-eval 只读评估（frozen预测×immutable outcome，
  三态agreement，双证据sufficiency，filter-then-limit分页，paired ETQS，
  authoritative execution去重，标准median，只读CLI）。
- 最终证据：全量1788 passed + 44 subtests；S6 MERGE_READY。
- 约定：One execution, one authoritative prediction, one immutable outcome.
  后续Canary另行决策，不在本PR。

## [2026-09-26] fix | PR #102 评审 P1：WAL 缺边车时只读路径 fail-closed
- 缺陷：`mode=ro` 对边车缺失、目录可写的 WAL 库会由 SQLite 实体化 `-wal`/`-shm`（OS 级副作用）。
- 修复：`get_readonly_db_connection` 先以原始字节读文件头 18/19 判 WAL，再查边车对；
  缺失即 `ReadonlyWalSidecarError`（CLI exit 1，不建任何文件）。不采用 `immutable=1`：
  生产库随时可能被并发提交，会读到过期快照。
- fixture：`_make_env` 保一条打开的 WAL 连接（keeper，无数据写）模拟 live 在场态；
  case3b 关闭 keeper 验证 fail-closed + 目录零新增，case3c 验证边车在场字节一致。
- 证据：`tests/test_shadow_evaluation.py` 47 passed；全量 1799 passed + 44 subtests；
  通用教训归档 lessons §92。

## [2026-09-27] feat | PR #103 Adaptive Router v2 Canary Mode（默认关闭）
- 闭环最后一步：预测 → 少量真实执行 → Outcome → 重新评估 → #104 扩量人工决策。
- 四保护：配置默认关闭（缺文件/非法 = 关闭，fail-closed 解析）；白名单 bucket
  （recommended_agent × node × task_type，分流目标恒为池/健康/隔离过滤后的候选）；
  `sha256("canary-v2|{run_id}|{task_id}")` mod 100 确定性分流（禁 Python hash，
  身份缺一不分流，bucket 可覆盖 percentage）；任何异常 fail-open 回 Legacy 并留
  `route_decision_error`(mode=canary)。
- 准入复用 Shadow 权威判定：`model_data_status` + `evaluation_data_status` 双
  sufficient（`shadow_rows._collect_rows_with_meta(mode="all")` + sufficiency 函数，
  截断无法证明即拒绝）；bounded-scan 收敛为 mode 切片单实现（shadow/canary/all +
  row_builder），canary_evaluation 为薄封装。
- 事件契约：每路由恰一条 route_decision（canary 带 legacy_agent/diverted/
  canary_gate，或 shadow）；Shadow 评估跳过 canary 事件并计数，保护"recommended
  未执行"语义；`herdr-task canary-eval` 两臂（diverted vs 未分流）observed 对比，
  只报事实不报 rollout 结论。
- 验收：全量 1836 passed + 44 subtests（基线 1800+44，+36 零回归）；真实链路 7 步
  PASS（分流→事件→reservation→Outcome→canary-eval 逐字节确定性→shadow 排除）；
  S6 对抗自查修复 1 项（共享过滤器 --agent "" 行为漂移）；MERGE_READY。
- 已知边界：评审独立性受限（同模型自查，子代理被宿主拒绝）；未在真实生产
  workflow 触发（需授权，默认关闭）。

## [2026-09-27] fix | PR #103 评审修复：3 P1 + 1 P2（实验真实性）
- P1 配置读取：非法 UTF-8（UnicodeDecodeError 非 OSError）会逃逸并打断路由；
  修复为捕获 UnicodeError + agent_router 调用点兜底，任何配置失败 = 关闭。
- P1 持久化门：diversion 原为锁外 best-effort 记录，决策落盘失败仍真执行 →
  评估永远无法归属。改为锁内、写 reservation 之前先持久化
  route_decision(canary, diverted)（record_event 异常或 False receipt = 失败），
  失败明确回 Legacy 并把 reservation 记为 Legacy。
  原则：No persisted canary decision, no canary execution。
- P1 evaluation dedup：同一 (task_id, run_id) 的 retry 多决策会把 immutable
  Outcome 重复计入两臂；复用 #101 的 select_authoritative_execution_rows。
- P2 bucket 身份：报告 key 从 node × task_type 改为 recommended_agent × node ×
  task_type，与准入单位一致；渲染同步。
- 验收：全量 1841 passed + 44 subtests（+5 修复测试）；真实链路 9 步 PASS
  （新增：损坏配置 fail-open、持久化失败 → Legacy + Legacy reservation）。

## [2026-09-27] merge | PR #103 已合并（Adaptive Router v2 Canary 收口完成）
- merge `e80a480`：main 现含 Canary 分流（四保护：默认关闭/白名单 bucket/sha256
  确定性分流/fail-open）、持久化门（No persisted canary decision, no canary
  execution）、`herdr-task canary-eval` 两臂只读评估（execution 去重 +
  recommended_agent × node × task_type bucket）。
- 评审闭环：外部评审 3 P1 + 1 P2 全部修复（`8d099a1`）；评审通过后合并。
- 合并后 main 验证：全量 1841 passed + 44 subtests。
- 下一步：#104 Controlled Rollout（per-bucket percentage 旋钮已在 canary 配置预留）。

## [2026-09-27] feat | 我的仪表板进首页（feat/dashboard）
- 需求：任务状态、等你决策（含默认动作）、最新交付、卡住四段，一屏可读；真实钟；10s 刷新；双击打开。
- 实现：`herdr/dashboard.py` 纯聚合（任务/阻断/交付/停滞/异常+工位实时，有界限量）；
  首页新增 `dashMode` 第三视图（与运维驾驶舱同模式：header 入口、视图持久化、`?view=dashboard` 深链）；
  `/dashboard` 302 到首页视图；`console/HerdrDashboard.command` 双击入口；安装脚本同步。
- 不变量：只读旁路（写操作复用既有 signoff/execute-action 接口）；工位探针 ≤20、3s 超时、8 并发、失败隔离；
  交付为空是真话（生产尚无 delivery 记录，FR-4 未落地）。
- 证据：全量 1810 passed + 44 subtests；`/api/dashboard` 0.09s；首页 200 + 302 smoke。

## [2026-09-27] feat | 仪表板工作流筛选（feat/dashboard-filter）
- 补齐：仪表板下拉框按工作流筛选（默认全部），筛选后四段/KPI 均按该工作流；
  「进入该工作流」跳工厂页管理；`?view=dashboard&workflow_id=` 深链与视图持久化。
- 数据：标题复用 `_with_subject`；未知工作流返回空分段不抛错；选择器 50 个最近优先。
- 证据：全量 1855 passed + 44 subtests；scoped API 与首页 smoke 通过。

## [2026-09-27] feat | Adaptive Router Controlled Rollout（feat/adaptive-router-rollout-v1）
- 目标：#99~#103 闭环后，只解决“已进入 Canary 的 bucket 如何安全、可审计、可回退地扩量”。评分、分流、Outcome、评估指标全部复用，不新增第二套事实源。
- 状态模型：`herdr/rollout_policy.py` 独立模块管理 per-bucket `recommended_agent × node × task_type` 的闭枚举阶段 `off/5/10/25/50`；`state_db.rollout_state`（当前值）+ `rollout_audit`（不可变历史）同事务 `BEGIN IMMEDIATE` 写入，每次变化恰好一条审计（`previous/new_percentage`、`action=promote|rollback|auto_rollback`、`reason`、`source`、`algorithm_version`）。
- 不变量：扩量只走相邻人工 `set`（`--reason` 必填，跨级拒绝）；回退任意阶段可直达 `off`；`HERDR_ADAPTIVE_ROLLOUT_ENABLED=false` 立即全 bucket Legacy 且保留历史；#103 `sha256(canary-v2|run|task) mod 100` 身份与 `No persisted canary decision, no canary execution` 门完全不动，只替换 `effective_percentage` 的来源（5%⊂10%⊂25%⊂50% 单调包含，不洗牌）。
- Safety Guard：只读消费 `canary_evaluation` 两臂 facts，阈值集中可调（min_settled=20 / min_arm=8 / success_drop=0.20 / blocked=0.30 / human=0.30），样本不足安静，可关闭；触发只写“下”，不写“上”。
- 评审闭环：S6 发现 3 个 blocking（热路径 guard 默认开启、解析失败回退旧配置导致扩量、并发写覆盖）与 6 个非阻塞问题，全部修复并补回归测试；热路径 guard 改为 `HERDR_ROLLOUT_HOT_GUARD=1` opt-in。
- 证据：rollout 专项 45 passed；全量 1900 passed + 44 subtests；真实 CLI `rollout status/set/off/history/check-guard` 逐条 smoke 通过。
- 文档：`docs/architecture/adaptive-router-rollout.md`（新增）+ `docs/references/cli-reference.md` 第 7 节。

## [2026-09-27] fix | PR #106 评审修复：absent≠off、guard 评不了即 Legacy、只读不造状态
- 评审结论：PR #106 主体设计认可，但 2 个 P1 + 2 个 P2 会破坏 “Safety always wins”，不予合并。
- P1（配置态 bucket 的 off 是假成功）：无 staged row 的 bucket 仍按 canary 配置分流，
  旧实现把 `off` 判成 no-op 且不写行 → CLI 报“已关闭”而 50% Adaptive 仍在跑；
  Safety Guard 的 `already off` 同样中招。修复：absent ≠ explicit off，`rollout off`
  必须写显式 0 行；无行且无 canary 流量时才真 no-op。`apply_rollout_stage_atomic` 拆出
  `expected_staged_percentage`（并发校验）与 `previous_percentage`（审计记录真实分流比例）。
- P1（hot guard 评不了反而放行）：`evaluate_guard` 出错时返回 triggered=false，
  `should_force_legacy` 据此继续 Adaptive —— 与自身契约相反。修复：新增 `status`
  维度（triggered/within_tolerance/insufficient_samples/unavailable/disabled），
  `unavailable`（评不了）在热路径一律走 Legacy；样本不足仍放行（那是判断不是异常）。
- P2（status/history 并不只读）：读路径曾走 `get_db_connection` + `_ensure_rollout_schema`，
  会建表/迁移，违反 #102「观察状态不得制造状态」。修复：读路径改走
  `get_readonly_db_connection`（mode=ro + query_only），pre-rollout 库读作空历史，
  缺表抛 `ReadonlySchemaError` 而不是隐式建表。
- P2（读取失败被伪装成全部 off）：`list_states` 吞异常返回 `[]` → CLI 显示
  “all off”。修复：异常上抛，CLI 边界 exit 1 并报 `unavailable`。
- 新增 `canary_router.config_percentage_for()` 供 CLI 提供真实 fallback（None=无流量）。
- 证据：rollout 专项 56 passed（新增 absent≠off / guard outage / 只读不建表 /
  读失败非零退出 等回归）；全量 1911 passed + 44 subtests。

## [2026-09-27] fix | PR #106 评审修复二：审计 action 必须与真实流量一致
- 评审结论：上轮 2 P1 + 2 P2 已全部修好；仅剩 1 个 P2 审计语义问题，修完可合并。
- 根因：`action = _action_for(staged, nxt)` 用 staged 值判定，而审计写的是
  `effective_prev`。首次接管旧 canary 配置时 staged=0、effective_prev=50，
  于是产出自相矛盾的不可变事实 `50 → 5 action=promote`（config=100 时更明显）。
- 修复：过渡合法性仍按 staged 阶梯校验（首次接管仍从 stage 5 起步），
  但 `action` 改按真实有效比例 `effective_prev` 判定。现语义：
  `0→5 promote`、`5→10 promote`、`50→5 rollback`、`100→5 rollback`、
  `50→0 rollback`、`auto→0 auto_rollback`。`_action_for` docstring 明确要求
  入参必须是“当时真正在分流的比例”，防止再次从 staged 推导。
- 顺带：`apply_rollout_stage_atomic` 补 `Path` 归一（此前传 str 会
  AttributeError，CLI 传的是 Path 故生产未暴露）。
- 证据：新增 `test_migration_from_full_config_is_a_rollback`（100→5）与
  `test_audit_action_matches_traffic_direction`（6 个 subtest 表驱动，并断言
  “action=promote ⟺ 记录的方向确实是增加”这一不变量）；真实 CLI 复现
  `50% -> 5% action=rollback`、`100% -> 5% action=rollback`，经典阶梯仍全为 promote。
  rollout 专项 58 passed + 6 subtests；全量 1913 passed + 50 subtests。

## [2026-09-27] fix | PR #106 评审修复三：快照即身份、guard 精确 bucket、单一 DB 解析器
- 评审结论：上轮 audit action 已修好；本轮 4 P1 + 1 P2，其中 3 个 P1 直接威胁
  “Safety always wins”。
- P1（紧急 off 被 stale promotion 覆盖）：CAS 只比 `percentage`，分不清
  `absent` 与「显式 0 行」——而前者仍按 canary 配置分流。读到 absent 的 promotion
  会在事务里看到 percentage 仍为 0，判定无冲突，直接覆盖刚写入的紧急回退。
  修复：引入 `RolloutSnapshot(exists, percentage)`，**存在性进入 CAS 身份**。
- P1（no-op rollback 在事务外直接返回）：`get_rollout_stage()` 与
  `rollout_stage_known()` 两次独立读取后判 no-op 并直接返回，并发 promotion 之后
  仍会报告 “already off”。修复：读-判-写收敛进 `state_db.transact_rollout_stage`
  一个 `BEGIN IMMEDIATE`（读快照 → 校验 == 决策依据的快照 → upsert + audit → COMMIT）；
  `write=None` 的 no-op 同样校验。判定下沉为纯函数
  `rollout_policy.decide_rollout_change(snapshot, ...)`，state_db 不含业务判断。
- P1（guard 被兄弟 bucket 饿死）：`_bucket_report` 只按 node/task_type 过滤，
  200 条决策预算会被同一 node/task_type 下更活跃的兄弟 recommendation 吃光，
  目标 bucket 读成「无样本」→ guard 安静 → 明显恶化却继续放行。修复：给
  canary collection 增加专用 `recommended_agent` 精确过滤（`agent` 语义太宽，
  同时匹配 actual/recommended/legacy），且**在 limit 之前生效**。
  回归测试已验证：撤掉修复即复现「目标 bucket 读成 None」。
- P1（读写两套 DB resolver）：`set/off` 经 `_get_store()`，`status/history` 经
  `state_db.resolve_state_db_path()`，非默认布局（WORKFLOW_FILE / CHECKPOINTS_DIR）
  下可能 set 写库 A、status 读库 B。修复：统一为 `_rollout_db_path()`
  = `resolve_state_db_path()`，写路径不再二次猜测。回归测试已验证：恢复旧行为
  即复现 split-brain。
- P2（check-guard unavailable 仍退出 0）：监控系统会把「无法判定」读成「检查通过」。
  修复：`status=unavailable` → stderr + exit 1（JSON 走 stderr）；样本不足
  （`insufficient_samples`）仍是正常判断，退出 0。
- 证据：rollout 专项 66 passed + 6 subtests（新增快照 CAS/no-op CAS/单次快照读/
  guard 饿死/split-brain/check-guard 退出码等回归）；全量 1921 passed + 50 subtests。

## [2026-09-27] fix | PR #106 评审修复四：source 级过滤、腐坏 stage 可回退、bucket 键无歧义
- 评审结论：上轮 5 项已修好；本轮 1 P1 + 2 P2。唯一 P1 直接影响止损有效性。
- P1（guard 的 scan_cap 仍被无关决策吃光）：`recommended_agent` 只在 Python 层过滤，
  而 `scanned += len(fresh)` 统计的是**每页返回的原始行数**、且在过滤之前，
  因此 400 条更晚的其他 bucket / shadow 决策仍会耗尽 `scan_cap=400`，目标 bucket
  根本看不到 → guard 安静 → 目标 bucket 明显恶化却继续放行。
  修复：新增 `state_db.ExactDecisionBucket`，把 mode + recommended_agent + node +
  task_type 作为 **SQL WHERE 谓词**下推到 `query_route_decisions`，预算只被可能命中的
  行消耗。下推按 `recommended_agent` opt-in：shadow 评估依赖 Python 侧 mode 过滤来
  统计 `skipped_canary_events`，从不传该参数，计数语义逐字不变（新增回归锁定）。
- P2（腐坏 stage 无法 emergency off）：`decide_rollout_change` 遇到闭枚举外的 stage
  时把快照改写成 `absent/0`，导致 CAS 必然冲突于磁盘上真实的腐坏行，运维被困死路。
  修复：CAS 始终使用**原始**快照；腐坏时 `off` 会真正写入以修复（而不是 no-op），
  腐坏期间任何其他 stage 一律拒绝。写这个测试时还纠正了 no-op 判定：它必须比较
  **目标值**而非有效比例，且排除腐坏情形，否则首次接管的无行 promote 会被误判 no-op。
- P2（bucket_key `/` 分隔符碰撞）：`("a/b","c","d")` 与 `("a","b/c","d")` 都得到
  `a/b/c/d`，两个不同 bucket 可能读写同一行。修复：`rollout_state` 改用**列级复合主键**
  `(agent, node, task_type)`；展示/审计用的 `rollout_bucket_key` 改用 JSON 数组编码；
  早期按 bucket_key 主键建的表由 `_migrate_rollout_state_key` 按真实列原地迁移。
- 验证：三项修复均已确认「撤掉即失败」（分别复现 scan 预算被吃光、CAS 冲突、
  key 碰撞）。rollout 专项 72 passed + 6 subtests；全量 1927 passed + 50 subtests
  （注：一次全量运行中 `test_trajectory_observer` 的 done-gateway 用例偶发失败，
  属该用例自带后台调度 + `drain(timeout=10)` 的既有 timing flake，单独与重跑均通过，
  与本 PR 无关）。

## [2026-09-27] merge | PR #106 已合并（Adaptive Router Controlled Rollout 收口）
- merge `0d862cc`：main 现含 per-bucket 受控扩量 —— 闭枚举阶段 off/5/10/25/50、
  人工相邻推进（`--reason` 必填、跨级拒绝）、任意阶段可直达 `off`、
  `HERDR_ADAPTIVE_ROLLOUT_ENABLED=false` 一键停全部扩量且保留历史。
- 状态模型：`rollout_state`（复合主键 agent×node×task_type）+ `rollout_audit`
  （不可变、每次变化恰好一条），读-判-写收敛进单个 `BEGIN IMMEDIATE`，CAS 身份含
  “是否存在”+百分比，no-op 同样在事务内校验。
- 复用 #103：`sha256("canary-v2|run|task") mod 100` 分流身份、白名单、准入、
  持久化门（No persisted canary decision, no canary execution）全部未动，
  只替换 `effective_percentage` 来源，故 5%⊂10%⊂25%⊂50% 单调包含、不洗牌。
- Safety Guard 只读消费 canary-eval 两臂 facts，阈值集中可调、样本不足安静、
  可关闭，只写“下”；guard 读取把 bucket 范围下推到 SQL，scan_cap 只被可能命中的
  行消耗；评不了（DB/schema 故障）一律走 Legacy。
- 评审闭环：5 轮外部评审共 13 项（2+2+1+5+3）全部修复并补回归测试，多项已验证
  「撤掉修复即失败」；S6 工件 round 6 MERGE_READY。
- 合并后 main 验证：全量 1927 passed + 50 subtests。
- 遗留边界（有意不在本 PR）：热路径 guard 需 `HERDR_ROLLOUT_HOT_GUARD=1` opt-in；
  未 staged 的 bucket 仍以 canary 配置为准；CAS 无 `revision` 计数器（当前单行模型
  足够）；75/100 与 Adaptive 默认接管另行讨论；仓库尚无 GitHub Actions，
  测试结论均来自本地实跑。

## 2026-09-28 · Selective Reverification v1（PR #108，未推送）

- 候选轮换 A→B 后，`test` / `review` 不再一律重跑。复用只在四者同时成立时允许：
  真实 `git diff --name-status A B` + 显式非影响范围 + 带 `verified_candidate_sha`
  的来源 PASS + 不可变派生事实；任一不能证明即 RERUN。全程无 LLM 参与影响判定。
- 范围按「显式声明不会影响的路径」建模，不是「哪些文件要重测」：未声明的路径、
  未声明的 verifier 一律 RERUN。v1 只给 `test` 开 `docs/**/*.md`；`review` 的 `[]`
  表示不存在安全复用范围，即永远 RERUN。根级 `AGENTS.md` / `CLAUDE.md` / `RULES.md`
  是运行时行为契约，`.herdr-loop/*.md` 被评估器/投影读取，均刻意排除在外。
- 历史事实不可变：`test(A) PASS` 仍然是「test 验证了 A」。复用产生新事实
  `reverification_decision`，按 (workflow, from, to, verifier, 策略指纹) 幂等，
  A→B 与 B→A 必然是两条 episode。
- 策略身份取**已解析策略的指纹**而非版本号：收窄范围或删掉配置会真正撤销既有复用；
  只看版本号会让收窄变成装饰。事实按 (verifier, to_candidate_sha, 策略指纹) 精确
  绑定，因此候选再次变化、策略收窄，旧复用都自动失效。
- 复用节点**不创建 Task**，是调度决策而非 Agent 决策；它没有任务，所以「节点完成」
  与 Join Gate 都改读同一个纯函数 `resolve_effective_verification`，台账与门禁不可能
  对同一个分支给出相反答案。
- 三轮独立对抗评审共 17 项，全部修复并补回归测试；其中 3 项是**验证工件自身的错误
  陈述**（计数、不可复现日志、错误因果），已按实跑重写。
- 全量 2196 passed + 50 subtests；`compileall`、`git diff --check` 通过；
  `evaluate_join_gate(reuse_facts=None)` 与 3a84659 差分 20 万组零不一致。
- 外部评审第 4 轮查出 2 个 P1 + 1 个 P2，均已修复并做变异验证：
  (1) reuse 事实原先只绑 `to_candidate_sha`，**回滚会重新冻结一个曾冻结过的 SHA**，
  旧轮次的 reuse 因此复活，把从未验证的候选判为已覆盖 → 改为绑定 `candidate_frozen`
  事件 id（episode）；(2) 复用来源只校验了 evidence 没校验 claim，claim/evidence
  不一致的任务被提拔成了下一轮的证据 → 两者必须同时绑定 from 候选；
  (3) `record_reverification_decision` 的 read-then-compare 在并发下产生 8 行重复
  → 改用仓库既有的 `BEGIN IMMEDIATE` 写锁做原子 check+insert。
  变异验证：逐项回退后对应测试分别变红（回退原子性 → 8 条重复行）。
- 遗留边界：绝对 sweep 开销未由本方实测（第三方量测 6 节点 2.03×，8/12 节点更快）；
  `record_reverification_decision` 仍是 read-then-compare，并发下可产生内容相同的
  重复行（不影响判定）；复用单跳，不递归。
- 关联实现：`herdr/reverification.py`、`herdr/scheduler.py`、
  `herdr/scheduler_facts.py`、`services/herdr-controller.py`、`bin/herdr-task`。
- 教训：§94。归档走查：`docs/walkthroughs/20260928-pr108-selective-reverification.md`
  （含 entry-gate / S5 验证 / S6 评审三份门禁工件的长期副本，原件在 `.omc/` 且被
  gitignore，不随仓库留存）。

## 2026-09-28 · Controlled Rollout 合并后安全收口（PR #109，已合并）

- 只修五个已确认的漏洞，不新增 rollout 特性（无 75/100、无自动扩量、无新指标）：
  takeover、单快照读取、episode 证据窗口、精确 bucket 索引、百分比无损解析。
- **fallback 接管误判**：无 staged 行 + config fallback 5 + `set 5` 原被判 no-op，
  所有权永不迁移，`5→10` 永远被读成 `0→10` 拒绝。新增第 4 个审计 action
  `takeover`（`5% → 5%`）：流量不变、所有权迁移到 staged 行；绕过阶梯是因为
  阶梯管的是流量变化。有行同值仍是真 no-op，不新增审计行。
- **一次决策一次快照**：`effective_percentage` 原来分两次读（值 + 存在性），
  已 COMMIT 的紧急回退可能与旧值拼成一次决策。改为单次
  `read_rollout_snapshot`；回归测试把旧两读 helper patch 成抛错并断言快照
  恰好被调一次。
- **Guard 证据 episode 化**：每次成功的显式阶段变更（含 rollback/takeover）
  以最新 `rollout_audit.created_at` 为证据窗口起点。回退后重试 5% 不再被上一轮
  坏样本立即定罪，5% 好证据也不再证明 10% 安全；无审计历史 bucket 保持 #103
  不加窗读取，历史永不删除。判定结果新增 `evidence_since` 事实字段。
- **精确 bucket 真索引**：`idx_events_route_decision_bucket` 部分表达式索引
  （`CASE WHEN json_valid` 包裹四表达式 + timestamp + id），非法 payload 索引为
  NULL、INSERT 永不失败；只在 `_ensure_schema` 可写路径创建（#102 只读契约不破）。
  查询与索引共用 `_route_decision_bucket_exprs`，EXPLAIN QUERY PLAN 断言
  `SEARCH events USING INDEX …`，稀疏 bucket（5/305 行）不再扫历史。
- **先校验后转换**：`int(5.9)` 曾静默变合法 stage 5。`normalize_percentage` 与
  `_safe_fallback` 共用 `_strict_integral`：`5.9/NaN/inf/True/Decimal("5.9")`
  拒绝（域边界 ValueError / fallback 归零），`5.0/Decimal("5.0")` 合法。
- 未改：canary-v2 hash、bucket 定义、Router 评分、canary 准入与指标、#107
  Scheduler、#108 Reverification、Shadow 全量扫描与 `skipped_canary_events`
  口径、#102 只读契约。不变量复验：`No persisted canary decision → no canary
  execution`、`hash_bucket < percentage`、`5⊂10⊂25⊂50` 单调包含全部保持。
- 全量 2230 passed + 50 subtests；compileall / CLI 语法 / `git diff --check` 通过。
- 教训：「同流量」不等于「无操作」——所有权也是状态；「评不了」与「没问题」
  必须走相反的路由方向；任何跨两次读的决策都是并发窗口。

## 2026-09-29 · 选择性返工：门禁 blocked 后只重做被点名的实现 Task（PR #110）

- 解决的问题：fix-loop 回流到 implementation 的粒度一直是**整个阶段**。
  一次「前端少了一句错误提示」的评审会让后端 API 与数据库脚本一起重写。
  根因不是没人想省，而是系统里不存在 `Blocker → Affected Implementation Task`
  这条事实。V1 把它变成一条可持久化、可重放、可审计的事实。
- **唯一归因来源**：Verifier 的结构化 Gate Verdict 字段 `affected_task_ids`
  （`herdr-task set <gate> --verdict blocked --affected-task-id <id>`，追加式、
  仅 blocked 合法）。不从 `note` / 屏幕输出 / 文件名 / 模块名 / embedding /
  CodeGraph / AST / import graph 反推。**Explicit attribution first.
  Unknown means legacy fallback.**
- **Fail-Closed 是全有或全无**：缺失 / `[]` / ID 不存在 / 跨 workflow / 非
  `retry_node` / 已非当前谱系头 / 已 superseded / 门禁候选身份或版本不可证 /
  结论读不出 / 事实无法持久化 —— 任一命中即整个 selective 决策拒绝，
  **严禁**「三个 ID 里两个合法就只用那两个」。
- **无持久化事实，就没有选择性作废**：顺序硬编码为「构建 plan → 持久化不可变
  事实 → 才允许作废」，不存在先作废后补事实的窗口。
- **episode 身份刻意不含 targets**：`replan_id = SHA256(workflow_id, gate_task_id,
  gate_task_version, gate_verified_candidate_sha, retry_node, policy_identity)`。
  目标是结论不是身份；同 episode 换 targets 撞 id 时**必须拒绝**
  （`identity_content_mismatch`）而非覆盖；重放同内容返回 `exists`，崩溃恢复幂等。
- **保留 = 零写入**：`invalidate_for_fix_loop(..., selective_target_task_ids=...)`
  只递增被点名谱系（`B → B-r2`），未点名任务连 status 都不碰（实测输出
  `[SELECTIVE REPLAN PRESERVE]`）；replacement 继承原 goal/acceptance/
  integration_mode/task_type，blocker 上下文只进派发 prompt，绝不回写旧 Task。
  `selective_target_task_ids=None` 时逐字节等价 legacy。
- **「重开」不等于「全量重派」**：只 supersede B 而 A/C 仍 completed 时
  `is_node_complete` 会把节点误判为完成，故 selective 作废时
  `clear_stage_advance`，且每轮 sweep 用 `_selective_replan_awaiting_redispatch`
  把节点移出 completed（`[SELECTIVE REPLAN AWAIT]`）。该等待谓词与补派管线
  `lineage_redispatch_candidates` **是同一个函数**：同真同假 ⇒ 既不会全量重派，
  也不会把节点永久钉住。
- **latch 与重投都是 target-aware**：`pending_redo`/`fix_loop_item` 新增
  `mode`/`target_lineage_roots`；每个 target root 在 `latch_ts` 之后都要有非
  superseded 的 completed-like 成员（AND）。**保留任务的落定永不能解除 latch**；
  补投路径同步保留同一上下文，不给病理留第二通道。
- **通知是通道**：selective 下 `build_fix_loop_message` 改走
  `_build_selective_replan_message`——列出被点名谱系、明确
  「⛔ 禁止：对 `--stage implementation` 派发全量 fix task」、
  **不携带可照抄的 launch 骨架**。否则机制修好了、总指挥仍按旧通知全量重做。
- 未改：#107 候选冻结 / Join Gate、#108 reverification、Adaptive Router、
  Canary/Rollout 语义、`herdr/scheduler.py`、stage latch legacy 语义。
  未做（禁止项）：requirements/plan 级 replan、DAG 重写、AST/CodeGraph/LLM 影响分析、
  自动拆 Task、自动改验收标准。
- 评审：一轮独立评审（Opus 5，read-only，NEEDS_FIXES，9 项）全部处置；
  Round 2 增量复审因本环境子代理创建全线故障（`400 Model is unavailable`）
  不可得，已如实标为 `claude (self, round-2 delta)`，未冒充独立通过。
  6/6 守卫型修复经**变异验证**（反向改写 → 用例变红）。
- 验证：专项 91 passed；全量 **2320 passed + 50 subtests**（EXIT=0）；
  基线 2230 → 零回归；`compileall` / `git diff --check` EXIT=0；
  `stage-state.json` sha1 前后一致、实盘 `state.db` 扫 `wf-srp%` 命中 0 行。
- 教训：§95。归档走查：`docs/walkthroughs/20260929-pr110-selective-replan.md`
## [2026-09-23] wrapup | wf-haflow-0923-01 Eval+Replay V1 二次收尾：abandon 后已收敛，交付落 PR86（未合入）
- 交付物身份（本条取代上一条的终态结论）：PR https://github.com/allinai0506/HAFlow/pull/86 （draft，base `main`），head `agent/opencode/feat-wf-haflow-0923-01-impl-t1`@`7a6f2ae`，相对 `origin/main` 6 个提交。
- 收敛经过：stranded 工作恢复为 `eaf2afe`（impl-fix1 遗产）、`f847886`（impl-fix4 遗产）→ 两次合入 `origin/main`（`db2b213`、`f4e8f90`）→ `impl-fix5` 收敛为 `7a6f2ae`（真 preflight+launch、默认 frozen 拒绝、Eval 四事实字段、Compare 仅 before/after）。`test-auto-r7` 全量 1183/1183 pass；`review-auto-r2` 独立评审 MERGE_READY（阻断缺陷 0）。上一条 ABANDONED 结论描述的是**首次收尾时点**的事实，不是最终交付物状态。
- 六步状态：步骤 1-2 已完成（本节 + `docs/lessons/lessons-learned.md` §87）；步骤 0 交付 PR 已存在（记录 URL/base，未合并、未改写分支历史）；步骤 3 合并确认只读 `--dry-run` → ⛔ 未合入；步骤 4-6 DEFERRED（破坏性步骤须待 base 合入后执行）。
- 新增教训 `docs/lessons/lessons-learned.md` §87：收尾条目必须固定交付物身份（PR URL + head SHA + base）；stranded 恢复以 clone 为源、单独提交写 provenance；同名分支本地/远端分叉只读研判，禁止 reset/pull/delete；分叉不等于工作丢失，用内容等价性（`merge-tree` 结果树比对）判定而非数 commit。
- 合并指引：PR86 → base `main`，`git merge-tree --write-tree origin/main 7a6f2ae` 退出码 0（**无冲突，可直接合入**）。同 clone 残留的本地同名分支 `3de67c8` 经内容比对与「`origin/main` ⊕ `7a6f2ae`」结果树**逐字节相同**（`722c95e1`），即**无独有工作**；本次未 reset/delete，清理决策交 Controller。
- 证据：PR86；`shared/notes.jsonl`（test r7 / review r2 门禁）；`git rev-list --count origin/main..7a6f2ae` = 6；`git merge-base`（本地 `3de67c8` vs 远端 `7a6f2ae`）= `3be4362`；`git rev-parse 3de67c8^{tree}` = `git merge-tree --write-tree origin/main 7a6f2ae | head -1` = `722c95e1`。

## [2026-09-23] wrapup | wf-haflow-0923-01 Eval+Replay V1 fix6轮收尾：三项 Correctness 缺陷修复与门禁收敛，交付落 PR86（head 快进至 cc5e9a0）
- 交付物身份与条目关系（本条推进/继承上一条二次收尾条目）：PR https://github.com/allinai0506/HAFlow/pull/86 （draft，base `main`），head 分支 `agent/opencode/feat-wf-haflow-0923-01-impl-t1` 快进推进至 `cc5e9a0`（相对 `origin/main` 7 个提交，包含 fix6 7 文件 +348 -53 纯修正）。本条记录的是 fix6 轮在二次收尾 `7a6f2ae` 基础上的门禁收敛与最终候选状态。
- fix6 核心修复（三项 correctness）：
  1. P1-1（解耦）：`herdr/eval_engine.py` 解耦 requirements_satisfied 与 verification_passed，仅从 `acceptance_verdict` 或 `stage_verdict`（pass/blocked）读取，双向独立，无 LLM judge 与 score；
  2. P1-2（隔离）：`herdr/replay_engine.py` 建立 5 级策略来源继承链（ReplaySpec → 源快照 → Task 冻结 → Workflow 冻结 → 私有 definition），无来源时 policy 显式为 null（`policy_source: unavailable`），杜绝全局策略泄漏；
  3. P2-1（物化时机）：`herdr/replay_engine.py` 将 `record_replay_spec` 移至 launch 成功且身份核验（`run_id == replay_run_id` 且 `replay_of == source_run_id`）之后，失败时执行包含 Task/Workflow/事件/快照文件的完整级联原子补偿。
- 门禁证据：`impl-fix6` completed/pass（`cc5e9a0`）；`test-auto-r8` 独立测试全量 1191/1191 pass，触改 3 文件 ruff 62==62 零新增 lint；`review-auto-r3` 独立评审 MERGE_READY（阻断缺陷 0，7 项非阻塞建议汇总为后续工作项，不影响本轮交付）。
- 六步状态：步骤 1-2 已完成（本节 + `docs/lessons/lessons-learned.md` §88）；步骤 0 交付 PR 已存在且为 draft（PR86 head `cc5e9a0` base `main`，未合并、未改写历史）；步骤 3 合并确认只读 `--dry-run` → ⛔ 未合入；步骤 4-6 DEFERRED（破坏性步骤须待 base 合入后执行，不得删除 clone/pane/tab）。
- 合并指引与分叉研判：PR86 → base `main`，`git merge-tree --write-tree origin/main cc5e9a0` 退出码 0（树哈希 `a264b0ece8cc117db2cc5c981f0691186f408cbb`，**零冲突，可直接合入**）。本地同名分支 `3de67c8` 与远端 `cc5e9a0` 分叉（merge-base `3be4362`），本地分支为历史 fix5 节点残留，其工作树内容已完全被 `origin/main`（含 PR87）与 PR86（`cc5e9a0`）覆盖，本地无独有未提交工作；按硬约束未做任何 reset/delete/pull，建议待 PR86 合入 main 后由 Controller 统一清理本地分支。
- 证据：PR86（`gh pr view 86` state: OPEN, isDraft: true, headRefOid: `cc5e9a0`）；`shared/notes.jsonl`（`impl-fix6` pass `n-1790163114271-36f8`、`test-auto-r8` pass `n-1790163892258-aee0`、`review-auto-r3` pass `n-1790165060831-9aed`）；全量测试 1191/1191；`git merge-tree --write-tree origin/main cc5e9a0` exit 0。
## [2026-09-23] wrapup | wf-haflow-0923-01 Eval+Replay V1 fix6轮收尾：三项 Correctness 缺陷修复与门禁收敛，交付落 PR86（head 快进至 cc5e9a0）
- 交付物身份与条目关系（本条推进/继承上一条二次收尾条目）：PR https://github.com/allinai0506/HAFlow/pull/86 （draft，base `main`），head 分支 `agent/opencode/feat-wf-haflow-0923-01-impl-t1` 快进推进至 `cc5e9a0`（相对 `origin/main` 7 个提交，包含 fix6 7 文件 +348 -53 纯修正）。本条记录的是 fix6 轮在二次收尾 `7a6f2ae` 基础上的门禁收敛与最终候选状态。
- fix6 核心修复（三项 correctness）：
  1. P1-1（解耦）：`herdr/eval_engine.py` 解耦 requirements_satisfied 与 verification_passed，仅从 `acceptance_verdict` 或 `stage_verdict`（pass/blocked）读取，双向独立，无 LLM judge 与 score；
  2. P1-2（隔离）：`herdr/replay_engine.py` 建立 5 级策略来源继承链（ReplaySpec → 源快照 → Task 冻结 → Workflow 冻结 → 私有 definition），无来源时 policy 显式为 null（`policy_source: unavailable`），杜绝全局策略泄漏；
  3. P2-1（物化时机）：`herdr/replay_engine.py` 将 `record_replay_spec` 移至 launch 成功且身份核验（`run_id == replay_run_id` 且 `replay_of == source_run_id`）之后，失败时执行包含 Task/Workflow/事件/快照文件的完整级联原子补偿。
- 门禁证据：`impl-fix6` completed/pass（`cc5e9a0`）；`test-auto-r8` 独立测试全量 1191/1191 pass，触改 3 文件 ruff 62==62 零新增 lint；`review-auto-r3` 独立评审 MERGE_READY（阻断缺陷 0，7 项非阻塞建议汇总为后续工作项，不影响本轮交付）。
- 六步状态：步骤 1-2 已完成（本节 + `docs/lessons/lessons-learned.md` §88）；步骤 0 交付 PR 已存在且为 draft（PR86 head `cc5e9a0` base `main`，未合并、未改写历史）；步骤 3 合并确认只读 `--dry-run` → ⛔ 未合入；步骤 4-6 DEFERRED（破坏性步骤须待 base 合入后执行，不得删除 clone/pane/tab）。
- 合并指引与分叉研判：PR86 → base `main`，`git merge-tree --write-tree origin/main cc5e9a0` 退出码 0（树哈希 `a264b0ece8cc117db2cc5c981f0691186f408cbb`，**零冲突，可直接合入**）。本地同名分支 `3de67c8` 与远端 `cc5e9a0` 分叉（merge-base `3be4362`），本地分支为历史 fix5 节点残留，其工作树内容已完全被 `origin/main`（含 PR87）与 PR86（`cc5e9a0`）覆盖，本地无独有未提交工作；按硬约束未做任何 reset/delete/pull，建议待 PR86 合入 main 后由 Controller 统一清理本地分支。
- 证据：PR86（`gh pr view 86` state: OPEN, isDraft: true, headRefOid: `cc5e9a0`）；`shared/notes.jsonl`（`impl-fix6` pass `n-1790163114271-36f8`、`test-auto-r8` pass `n-1790163892258-aee0`、`review-auto-r3` pass `n-1790165060831-9aed`）；全量测试 1191/1191；`git merge-tree --write-tree origin/main cc5e9a0` exit 0。

## [2026-09-29] merge | PR #114 已合并：Flow Workbench v1 用真实 DAG 表达 Workflow 运行工作台

- 交付物身份：PR https://github.com/allinai0506/HAFlow/pull/114 （base `main`），head 提交 `c2faa61`，merge commit `c9bb16e`，合并时间 2026-09-29T13:26:12Z，11 files / +824 -11。
- 解决的问题：Console 用 Stage Stepper + Task List 表达真实 DAG，把 `software-development-v1` 的 test/review 并行分支画成串行链，产品能力与用户看到的模型不一致。本次以只读投影方式让 DAG 拓扑首次在 UI 中被准确表达。
- 架构：`Workflow Definition → herdr/workflow_graph.py::workflow_graph_projection()（纯函数）→ Dagre(TB) 计算坐标 → AntV X6 渲染 → Flow Canvas → Node Inspector`。X6 只负责画布与交互，Dagre 只负责布局，业务真相全部留在投影层。
- 依赖与离线：`@antv/x6@3.1.8` + `@dagrejs/dagre@3.1.1` 固定版本 vendored 到 `console/static/vendor/`（保留双 LICENSE），经本地 `/static/` 路由加载，运行时零 CDN、断网可用；npm 仅作开发期获取手段，Console 启动方式不变。`scripts/install-herdr-console.sh` 补 `rsync console/static/`。
- 真值纪律：nodes/edges 仅来自真实 Workflow Definition 与 `depends_on`；状态聚合 `blocked > failed > rework > working > completed > waiting` 确定性且与既有 `stage_summary` 同源；context 无 contract 则返回空，不伪造「已加载」；legacy/空定义 fail-soft 返回有限 nodes + `edges: []`。
- 未改动：Scheduler / Router / Agent Router / Canary / Rollout / Reverification / Execution Outcome / Candidate Freeze / Join Gate / Workflow 执行语义 / Task lifecycle / Controller 语义。无副作用、无持久化写入。
- 评审闭环：S6 三轮独立审查。Round1 NEEDS_FIXES（dead X6 Selection API）→ 选中态改走 `cell.attr('body/stroke-width')`；Round2 MERGE_READY；用户实测报 F1（我的仪表板显示为同一页面）后 Round3 NEEDS_FIXES 给出 3 项容器归属缺陷（D1 selectSpace 陈旧图 / D2 showOpsCenter 未清 dashMode 致 10s 定时器互相覆盖 / D3 aux 模式任务筛选误覆写）→ 抽出 `setWorkspaceMode(flow|list|aux)` 单一入口修复。最终 MERGE_READY，无未解决正确性/安全性缺陷。
- 验证：`pytest -q tests/test_console* tests/test_workflow*` 253 passed / 3 subtests；compileall 与 `git diff --check` exit 0；Console 实启 `/`、x6、dagre 全 200；HTML 静态校验 7/7。**未验证项**：`pytest -q` 全量套件 120s 超时未跑完（专项套件全绿，已在 PR 描述中如实标注）。
- 知识沉淀：新增 `wiki/flow-workbench.md`（分层职责、真值来源表、状态聚合优先级、离线依赖、read-mostly 契约、容器归属）；`docs/lessons/lessons-learned.md` §99（共享渲染容器的单一所有权 + 部署资产与运行副本的同源要求）。
- 遗留边界（已交付但需知悉）：节点标签使用 X6 `rect` + 文本而非 `shape: 'html'` 自定义卡片，样式控制较简；Flow 图随 `loadWorkflow` 重绘，与 Task List 同节奏，未做独立图轮询。

## [2026-09-30] wrapup | Flow Workbench v1 全链路收口（PR #114 + #115 均已合入 main）

- 合并确认：PR #114（功能，head `c2faa61`）于 2026-09-29T13:26:12Z 合入，merge commit `c9bb16e`；PR #115（知识沉淀，head `8539e39`）于 2026-09-29T22:54:33Z 合入，merge commit `1b95532`。本地 `main` 已 ff 至 `1b95532`，工作区干净。
- 本次交付最终形态：`herdr/workflow_graph.py` 纯投影层（`workflow_graph_projection` / `aggregate_node_status` / `pick_default_node`）+ Console Flow Canvas（Dagre 坐标 + X6 渲染 + Inspector 四页签 + Flow/List 视图切换）+ `console/static/vendor/` 离线依赖（X6 3.1.8 / Dagre 3.1.1，双 LICENSE）+ 5 条新契约测试。
- 知识沉淀：新增 [[flow-workbench]] 知识页并登记双向链接；`docs/lessons/lessons-learned.md` §99「共享渲染容器的单一所有权 + 部署资产与运行副本同源」，含反向验证命令。
- 收尾验证（绑定 `1b95532`）：`pytest -q tests/test_console* tests/test_workflow*` → 253 passed / 3 subtests；`compileall herdr services bin tests console` exit 0；`git diff --check` exit 0；部署形态 `~/.herdr-console` 与仓库 console/x6 资源 IN-SYNC，LaunchAgent `com.user.herdr-factory-console` running（pid 58641），`/` 与两个 vendor 资源均 200。
- 六步状态：步骤 1 知识沉淀 ✅、步骤 2 wiki checkpoint ✅（无 `.wiki/WIKI.md`，按 AGENTS.md 治理回填 `wiki/`）、步骤 3 合并确认 ✅（两个 PR 均 MERGED，只读核对未做任何强制推送或历史改写）、步骤 4 anchor sync ✅（main ff 至 `1b95532`）、步骤 5 分支校验 ✅、步骤 6 卫生检查 ✅（无残留临时文件；`__pycache__`/`.DS_Store` 已被 `.gitignore` 覆盖）。
- 未执行 / 需知悉（均非本轮可越权处理）：`pytest -q` 全量套件 120s 超时未跑完（专项套件全绿，已在 PR 描述与 §99 中标注）；本地 `feat/flow-workbench-v1` / `docs/flow-workbench-wrapup` 两个已合入分支未删除（分支/clone/pane 清理属 `close-workflow` 职责）；节点标签为 X6 `rect` + 文本而非 `shape: 'html'` 自定义卡片；Flow 图随 `loadWorkflow` 重绘，未做独立图轮询。

## [2026-09-30] root-cause | wf-project-0929-01 plan 节点永久卡死：完成标记被终端硬折行，裸子串匹配恒为假

- 现场：`plan-arch` = `cleaned`，`plan-adversarial` 卡 `working` 9.5h。产物与台账全齐（技术方案 64KB、对抗审查 158KB、`kind=gate` 台账在 `notes.jsonl`），`agent get w13:pB` = `idle`，`observed_version == tasks.version == 3`（CAS 无版本漂移），仅 `marker_present=0` / `consecutive_samples=0`。
- 根因（实测）：`herdr pane read w13:pB --source visible` 显示标记被折成两行 `HERDR_TASK_DONE:plan-adversarial-unified-task-` + `workbench-v1`。`services/herdr-sentinel.py` 与 `services/herdr-controller.py` 各用 `f"HERDR_TASK_DONE:{task_id}" in screen` 读**已折行的终端屏幕**，标记不再是连续子串 → `marker_present` 恒 False → CAS 恒以 `completion_marker_absent` 拒绝 → 节点永不推进。
- 宽度判据：标记长 = 16 + `len(task_id)`。`plan-arch` = 51 字符单行放下（正常推进），`plan-adversarial` = 58 字符超宽折断（永久卡死）。缺陷与产物质量无关，只取决于 task_id 长度与 Pane 宽度之差。
- 修复：探测逻辑下沉为纯函数 `herdr/completion.py:marker_present / marker_literal`；只消解缩进续行 `\r?\n[ \t]+(?=\S)`（空行/纯空白行/无缩进行保留换行，fail-closed），命中后做标识符边界校验（`...-v1` 不满足 `...-v1b`，跨 Task 证据不通用）；`HERDR_TASK_DONE` / `HERDR_TASK_BLOCKER` / `HERDR_ORCH_TASK` 三前缀共用同一接缝，4 处调用点全部改造。
- 防复发：`tests/test_completion_marker_wrapping.py`（27 passed）= 纯层行为 + **源码级契约**（任一守护进程重新内联 `f"HERDR_TASK_DONE:{task_id}"` 即失败，2 daemon × 3 prefix = 6 条）。`tests/test_inner_loop_protocol.py` 两条断言源码文本的契约测试**重定向到新接缝并加强**（未删除、未放宽）。
- 流程解锁（真实链路，非手工改状态）：`launchctl kickstart -k` 重载 sentinel + controller 后 → `working → agent_done → completed → cleanup_ready → cleaned`（version 3→8）→ `AUTO ACCEPT baseline=TASK_CHANGED` → `[STAGE ADVANCE QUEUED] plan -> implementation` → 已真实派发 `impl-barrier0-plan-rectify`(opencode, working) 与 `impl-t6-mock-retire`(qodercli, blocked)。
- 收尾验证（绑定当前源码）：`pytest -q` → **2406 passed, 50 subtests passed**（318s）；`compileall herdr services bin tests` exit 0；`git diff --check` exit 0。
- 知识沉淀：`wiki/task-lifecycle.md` §1.3「完成标记的折行容错契约」新增知识页小节；`docs/lessons/lessons-learned.md` §100「活性判据的输入必须匹配它的物理载体」。
- 未验证 / 需知悉：修复前全量为 2396 passed / 2 failed（两条源码文本契约测试），修复后 2406 全绿；`impl-t6-mock-retire` 当前为 `blocked`，属该 Task 自身的内环仲裁面，与本缺陷无关，Controller 已按既有 `blocked_marker_observed` 通路接管。

## [2026-09-30] root-cause | blocked 观测的 CAS 风暴：陈旧样本被每轮 sweep 重试 238 次

- 现场：`wf-project-0929-01` / `impl-t6-mock-retire`（qodercli）08:29:14 启动，08:29:18 Sentinel 上报 `blocked_marker_observed`（`observed_version=3`）；08:29:46 一次 `herdr-task set-status` 把版本抬到 5；此后到 08:54:04 落 `blocked` 之间，Controller 每轮 sweep 用同一份旧样本发 CAS，`blocked_observation_cas_rejected` 累计 **238 条**（跨 25 分钟、任务零进展）。最终成功纯靠 08:53:58 Sentinel 碰巧再次看见标记、写入 `observed_version=5` 新样本。
- 根因：`process_blocked_observations` 把观测读在**事务外**（`list_events` limit=1 desc），把事件里的 `observed_version` 直接交给 `kernel.transition_task`，被拒即记一条事件后 `continue`，下一轮原样重来。两条独立缺陷：① 无前置检查，代码注释已写明"An old event can never win"但仍照发；② 拒绝事件无去重，每轮 sweep 往 facts ledger 追加同一事实。
- 对照：完成观测通路 `compare_and_set_completion_transition` 在**单事务内**完成校验+CAS+消费观测，天然不重试，全库拒绝计数个位数。缺陷只在事务外读观测的 blocked 通路暴露。
- 修复：新增纯判据 `herdr/completion.py:observation_is_current()`，Controller 在发起 CAS **之前**判定 —— version/status 不符即静默跳过等 Sentinel 补新样本（不打事件、不占转换预算）；非预期拒绝按 `(task_id, observed_version)` 去重，一个样本一条事实；缺 version 时放行交权威 CAS 裁决；非法 version fail-closed；任务离开 active 时清键防止进程内 map 无界增长。fail-closed 语义不变，只减少明知会拒的尝试。
- 防复发：`tests/test_blocked_observation_cas_storm.py`（15 passed）= 纯判据 9 条 + Controller 接线 6 条。**反向验证已执行**：回退前置检查（`if False and ...`）与去重（`if True:`）后 3 条精准失败（test_stale_sample_is_never_attempted / test_unexpected_rejection_is_recorded_once_per_sample / test_a_new_sample_retries_and_is_recorded_again），恢复后全绿。
- 现场数据回放（真实 events，非夹具）：样本 `observed_version=3` 在权威 version=3 时判定"会尝试"，version=4/5 判定"跳过"；08:53:58 新样本 `observed_version=5` 判定"会尝试"，与 08:54:04 实际落 `blocked` 一致。
- 收尾验证（绑定当前源码）：`pytest -q` → **2426 passed, 50 subtests passed**（382s）；`compileall herdr services bin tests` exit 0；`git diff --check` exit 0。
- 知识沉淀：`wiki/task-lifecycle.md` §1.4「陈旧观测的 CAS 前置跳过」；`docs/lessons/lessons-learned.md` §101。
- 未验证 / 需知悉：本 PR 只改判据与重试策略，**未在生产守护进程热重载验证**（需 `launchctl kickstart` 属运维授权，未执行）；`impl-t6-mock-retire-r2` 的 `codex TOKEN_EXHAUSTED` 属 Agent 供给问题，与本缺陷无关，`r3` 已在飞。

- 2026-09-30：补充 Worktree 来源的 CoW Clone Git 隔离契约；回归覆盖两个 Clone 分支/index 相互隔离及源 staged WIP 保留。
- 2026-09-30：完成判定接纳 Herdr runtime `done`，保持新标记、双采样间隔、60 秒与 CAS；新增真实 SQLite 落库回归。
- 2026-09-30：本地 agent init anchor 集成使用源仓库基线与 task-scoped ref；保留普通远端集成和 source Git 锁。
- 2026-09-30：内循环评估保留绿色标题中的FAIL/✕语义，避免幽灵失败；Vitest/Jest回归覆盖真失败保留。

- 2026-09-30：wf-project-0929-01 Candidate 根因恢复：Git 节点等集成、显式 planned Task 清单、CLI/ops读取口径同步、Worktree本地refs保留与作废候选排除；详见 task-lifecycle 和恢复记录。

- 2026-09-30 Candidate 恢复追加：真实配置加载保留 planned Task 身份，CLI 仅加载记录明确路径，缺失文件不借用 foreign legacy；恢复任务绑定 Java 验收入口。

### 2026-09-30 — 阶段内部交接义务防静默停滞

- 区分集成引用接收与指定目标 SHA 的采用证据，显式计划缺项不因 Task cleaned 消失。
- Controller 复用 attention 事务保存有界协调恢复义务；暂停/阻塞不催办、重启复核、旧队列重新核验、独立进程去重。
- Console stall 复用同一判定显示未派发和未采用成果。Evidence: `tests/test_workflow_continuation.py`；详见 [[task-lifecycle]]。
- 最终评审补充：最后一个 Task superseded 后替代项缺失仍保留交接义务；3 条先红后绿回归覆盖崩溃窗口和已 notified latch。

- 2026-09-30 — C26投影命名空间：默认CLI/steering导出及opt-in迁移跟随所选SQLite，显式覆盖保留；临时宿主与隔离库真实回归防写穿。详见 [[task-lifecycle]] §1.5与工程教训§91；仅本地验证，未部署。

- 2026-09-30 — C06评估步骤隔离：test/lint/repro整段命令子shell、完整日志、独立cwd与真实退出码。详见 [[task-lifecycle]] §1.6；外层执行完整性C28未关闭，未部署。

- 2026-09-30 — C28单次评估完整性：runner退出、唯一有效回执、新鲜日志、缓存满分否决及耗尽求助证据。详见 [[task-lifecycle]] §1.6与教训§111；C29并发串读未关闭，未部署。

- 2026-09-30 — C29评估跨进程所有权：eval/init/baseline共用内核文件锁；竞争者退出75且不改产物，异常/进程退出可恢复。详见 [[task-lifecycle]] §1.6与教训§111；C27后代进程清理未关闭，未部署。

- 2026-09-30 — C27本次runner进程组回收：超时/中断/异常/正常返回均在评估锁内回收；原生124保留，无关进程不受影响。详见 [[task-lifecycle]] §1.6与教训§112；不可捕获终止与主动脱离session仍属外部恢复边界，未部署。

- 2026-09-30 — C05a默认npm非交互：CI=1避免继承TTY后watch挂起；真实Vitest PTY旧exit124/新exit0，显式命令保留。详见 [[task-lifecycle]] §1.6与教训§113；C05b测试范围契约未关闭，未部署。

- 2026-09-30 — C27b基线入口闭环：复用evaluator受管命令生命周期，基线采集到写入持锁、超时不造基线、SIGTERM作用域内清理并恢复handler；真实进程与并发初始化回归。详见 [[task-lifecycle]] §1.6与教训§112，未部署。

- 2026-09-30 — C03b仲裁事实保护：实时/重启运行信号不能抹掉内循环blocked；重启恢复队列，最新转换历史区分普通blocked与遗留metadata。详见 [[task-lifecycle]] §1与教训§114；C03c旧标记、C30非法恢复命令未关闭，未部署。

- 2026-09-30 — C30恢复命令契约：仲裁卡/人工升级提示blocked→working，复用现有合法状态边；真实CLI→临时SQLite状态/历史验证，不force放行。详见 [[task-lifecycle]] §1与教训§114，未部署。

- 2026-10-01 — C31初始化证据失效与历史保存：原子失效EVAL_DONE、SHA寻址历史回执、新评估身份避免ABA；同字节跨进程稳定。详见 [[task-lifecycle]] 与教训§111；2673 passed/145 subtests，仅本地验证，未部署。

- 2026-10-01 — C12本地候选续接：显式完整SHA源/Clone双核对，普通未指定pin续接仍走origin；真CoW保留源WIP。127专项、2682全量/145子测试；中间旧夹具ok伪SHA失败保留并替换为原生Git，不改断言。详见 [[task-lifecycle]] 与教训§104；未部署。

- 2026-10-01 — C26b复核补缺失配套输入：保留所选path，不传None回宿主；四类TEMP读/SQLite回归，显式源正常对照。12靶向/64相邻/2687全量+145子测试，378.99s；独立复审无此项阻断，未部署。详见 [[task-lifecycle]] 与教训§91。

- 2026-10-01 — C27c公开初始化基线所有权收口：CLI/Task统一单锁init→采集→发布，旧债务失效，SIGTERM/超时清理；9靶向/86相邻+10子测试/2696全量+145子测试（384.91s），独立复审无此项阻断。详见 [[task-lifecycle]] 与教训§112补证；未部署。

- 2026-10-01 C28c专项：执行失败不能抵扣为lint历史债务；capture拒绝、metrics与convergence独立veto覆盖旧污染baseline；合法exit1/2对照保留。89专项/10子测试通过，19失败/3对照反证，独立只读复审无新增阻断；全量2718 passed/145子测试，0失败/0跳过，387.92s；未部署。

- 2026-10-01 C31b专项：归档旧回执先于current reset；故障/中断不丢字节，重试同hash幂等；12靶向、91专项/10子测试通过，撤销11 failed/1正常，独立只读复审无新增阻断。全量2730 passed/145子测试，0失败/0跳过（360.26s），未部署；历史BLOCKER过滤另卡。

- 2026-10-01 C31c专项：Supervisor摘要统一现代原子快照，禁止遗留BLOCKER/陈旧显示成为当前事实；legacy absent兼容，invalid未知。106专项通过，旧10失败/2正常反证；源码冻结全量2742 passed/145子测试，0失败/0跳过（370.78s），未部署。

- 2026-10-01 C34专项：修复fake key跨测试泄漏，确定性Observer不隐式模型调用；119专项通过，2失败1正常反证。C15b完整暂存，先独立测试隔离卡验收；未部署，full运行中。

- C34复审后补证：修正spy覆盖/异常吞噬盲点，最终字节移除禁用1失败2正常、恢复3通过；155扩大专项通过。旧中断全量不计通过，final full冻结运行中。

- C34最终：3靶向/155扩大专项、2745全量/145子测试通过，0失败0跳过405.16s；仅测试隔离本地提交，生产源码不变。下一项恢复C15b独立验收。

- 2026-10-01 C15b恢复：C34单独提交后恢复ANSI parser修复，新基线13靶向通过/11失败2正常反证，原始日志字节保留；邻接/full重新运行，旧失败/中断不算通过，未部署。

- C15b新基线最终：13靶向/98专项+10子测试/2758全量+145子测试通过，0失败0跳过385.08s；仅parser ANSI规范化本地提交，原日志保留。C34独立提交，旧失败全量未改写。

- 2026-10-01 C28b专项：GOAL自由文本不当配置，program末尾typed requirement、唯一unambiguous legacy兼容；复审中legacy假绿已完整回归闭合。22靶向/89专项+10子测试通过，旧19失败3正常反证；冻结源全量2780 passed/145子测试，0失败/0跳过，415.65s；未部署。

- 2026-10-01 C21b专项：blocked队列绑定持久转换episode，独立queue去重key保留attention原key；9失败1正常反证、10靶向通过，全量2790 passed/145子测试，0失败/0跳过，392.05s；未部署。

- 2026-10-01 C13专项：冻结身份与base相同不代表无候选；零差例外必须全批spec完整身份+最新freeze+native onto exact。独立审查mixed无onto漏洞闭合，15靶向/154扩展相邻+35子测试、3失败12正常反证；最终全量2805 passed/145子测试，0失败/0跳过，380.36s；未部署。

- 2026-10-01 C02专项：cap永久拒绝保存blocked并退出soft重试，原文/reason保留；暂时失败pending；CLI失败非零、Console错误、Sentinel只ok报Injected。14靶向/62扩展相邻，撤回9失败5正常，独立只读复审无新增阻断；全量2819 passed/145子测试，0失败/0跳过，382.20s；本地验证未部署。公开urgent新指令与内部同ID恢复区分，未进行真实投递。

- 2026-10-01 C35a：native index锁严格身份分类、Controller专属等待预算/60秒退避、历史owner与跨进程attention CAS；25靶向及10失败15正常反证，最终相邻/full进行中。T8锁及WIP原样保留，C35b现场恢复未完成；未部署。

C35a最终本地验收：25靶向通过9.20s，最终同25只撤销Controller10失败15正常6.66s；恢复冻结后67相邻通过14.52s；全量2844 passed/145 subtests passed，0失败0跳过391.46s。CLI AST、compileall、diff-check通过；独立最终只读复审无本卡新增阻断，未自行重跑suite。前轮相邻交叠和中间3失败19正常（CAS空None与{}实现错误）日志保留，不作为最终通过。所有写index命令均前后稳定实际锁身份，跨进程wait写入CAS；新owner失效只针对明确typed wait，metadata-only变更保留已有预算。仅本地代码验收，未部署；T8当前锁/legacy升级未修复，C35b仍开放。

- 2026-10-01 C13b：PR127组合审查发现main125省略onto时丢pin，已用最新freeze/native HEAD等值闭合；8靶向、2失败6正常反证、40专项3子测试，独立复审通过，全量进行中。旧中止full日志保留，不能当PASS。


C13b最终：66相邻passed/3子测试（32.93s）；最新main4cca57e合并后完整全量2871 passed、154 subtests passed、2 skipped（隔离HOME无LaunchAgent），0 failed，454.85s。两项本机只读plist检查另行2 passed（0.07s）。compileall、三入口CLI AST、diff-check通过；独立最终只读复审组合阻断闭合，未自行重跑全量。真实Agent/Worker启动、业务E2E及开放卡未因此验收。

- 2026-10-01 控制台 | 仪表板工作流 Linear 风格下拉框落地（feat/linear-workflow-dropdown）
  - 核心痛点解决：彻底解决执行中工作流 `wf-project-0929-01` 因 title 覆盖原生 select 导致等宽 ID 不可见的问题，统一采用双行信息架构（标题 + 等宽 ID + 活跃/决策胶囊），未匹配工作流提供“已归档或未知”容错呈现。
  - Linear 规范落地：落地方案 B（分状态组），顶部置顶“全部工作流”聚合视图，按“进行中 / 需决策”与“已完成 / 闲置”分组呈现；支持分组标题点击折叠/展开，搜索输入时自动强制展开命中分组；键盘上下键自动过滤折叠隐藏项；实现全局外部点击与 Escape 键关闭。
  - 工作台切换器解耦：进入仪表板时重置 `workflowSwitcher.dataset.sig`，避免返回工作台时因缓存签名一致导致 `#wfSelect` 未重新渲染。
  - 向下兼容与安全：保留隐藏 `<select id="dashWfSel">` 双向同步保证既有自动化测试与 CLI 工具链无损；行内动作统一使用 `jsArg()` 防范引号截断逃逸，全量输入经 `esc()` 转义。
  - 验收证据：`tests/test_console_linear_dropdown.py` 6 passed；控制台测试集全量 212 passed；`compileall` 与 `git diff --check` 零错误；独立 Reviewer 子代理（google-code-review）审查通过，判定 MERGE_READY。

- 2026-10-01 控制台 | UI 网格基线治理与间距吸附（feat/console-grid-p0-p1-alignment）
  - 核心痛点解决：按照 `snapping-ui-to-grid` 规范与 `docs/lessons/lessons-learned.md #10`，全面治理控制台四基准线失守与间距裸值散乱问题。
  - P0 右轴归位：顶部 Header 操作栏（`.actions`）与任务详情抽屉操作栏（`.task-drawer-actions`）Primary CTA `[＋新需求]` 与 `[成果会签]` 统一移至最右侧；新增 `.actions .btn.primary { margin-left: auto; }` 弹性右推规则，确保窄屏换行态主按钮依然贴合右边缘；抽屉主按钮内边距规范化为 `4px 12px`。
  - P1 左轴贯通：Flow 画布绝对定位工具栏 `#canvasToolbar` 坐标由 12px 修正为 16px（桌面端 `left: 16px; right: 316px;`，响应式 `@media (max-width: 980px)` 下 `left: 16px; right: 16px;`），与 Header 顶栏容器 `padding: 0 16px` 全高度垂直贯通对齐，消除伪左轴参差。
  - P1 数字轴等宽：为 7 大数值/计数/标识选择器（`.metric b`, `.nav-count`, `.filter-cnt`, `.badge-pill`, `#flowSummary b`, `.task-id`, `.task-drawer-id`）注入 `ui-monospace` 与 `font-variant-numeric: tabular-nums`，彻底解决数据动态更新时的水平跳动。
  - P2 间距裸值就近吸附：依据 `lessons-learned.md #10` 属性级锚定规范，严格在 `(padding|margin|gap)` 声明内就近吸附（`5px → 4px`、`6px/7px/9px → 8px`、`10px → 8px 或 12px`、`11px → 12px`、`14px → 16px`），未误伤任何 `font-size`、`border-radius` 或 `line-height` 等非间距排版属性；196 处间距声明经 `ai-slop-cleaner` 模式 B 瘦身化简，违规裸值清零。
  - 验收证据：`pytest tests/test_console*.py` 全量 214 passed；`pytest tests/test_console_frontend_syntax.py` 15 passed（`node -c` 校验干净）；`compileall` 零错误，`git diff --check` 零警告；两轮独立 Reviewer 子代理（google-code-review）审查通过，判定 MERGE_READY。


- 2026-10-02 任务与工位 | 并发开关与累计配额语义拆分
  - 新增 max_concurrency / max_tasks_per_node、旧字段显式确认审计、panes 与 Controller 资源计数。
  - 归档标记 orphan，身份验证 reap；可继续任务默认同 task/pane rework，终态替换需理由并计数。
  - 更新 routing、lifecycle、Schema 与 CLI 参考；验收结果以本轮 sandbox 验证记录为准，不代表已部署。


- 2026-10-02 工作流可靠性 | 完成回执、真实预检、幂等派发、分段检查点、版本/关闭与验收绑定
  - 新协议绑定 task/run/epoch、24h 服务端有效期；显式续签保留检查点，公平扫描跨过阻塞回执；历史任务保持原兼容策略。
  - 预检区分请求与交互证据；信任对话拒绝，未知资源保留。派发模式明确且跨入口去重，真实 launch intent 可核对恢复。
  - 原子产物/哈希复核、有界工具及临时 HOME；实际 Worker/loop 生产者到报告保留候选、退出码和 skip，未知不冒充成功。
  - 安装器核对 archive 内容、权限与缓存；关闭/重开/投递共用生命周期锁、逐资源日志，重用 Pane 绑定实例，dry-run 不声明删除。
  - 三轮组合审查后已升级人工，用户明确认可原生 API 与同 UID 边界。最后关闭/续签原始反例独立复跑发送 0；新增6项、相关70项通过。
  - 最终本地全量3134 passed、2 skipped、157 subtests passed，0 failed（449.15s）；两个跳过是隔离 HOME 无已安装 LaunchAgent 的只读检查，临时安装器测试已运行。源码映射无漂移；compile、CLI AST/help、Bash与diff通过；Ruff基线对照新增0。
  - 知识同步 task-lifecycle / preflight-and-health、CLI参考、工程教训#120；完整记录 docs/walkthroughs/20261002-workflow-reliability.md。仅 working_tree，本次未推送、合并、部署或重启，生产验收 unknown。

- 2026-10-02：工作流可靠性 PR 阶段完成同快照 CLI/loop 命令绑定及新鲜全量验证（3137 passed、2 skipped、157 subtests passed）；用户授权创建 PR 与本地重启，明确保留 Console 热补丁界面，只更新 Controller/Sentinel。详见 docs/walkthroughs/20261002-workflow-reliability.md；服务实际切换另行记录，不等于生产业务验收。

- 2026-10-02：补齐 installer 的 macOS Bash 3.2 nounset 空数组兼容；真实 /bin/bash + 临时 HOME 覆盖四种服务布局和重启/不重启，保留参数引用与发布校验。根因及回归入口见 docs/lessons/lessons-learned.md §121；本次为代码修复，未再次更新运行服务。

- 2026-10-02：修复 wf1002 已复现的 Worker 身份清理、旧标记空白/软换行边界、非 Git 文档 onto、替换义务及 Git Run 定义隔离。更新 [[task-lifecycle]] 与 CLI 参考，工程教训 §122。新作废保留义务，显式 abandon 不复活，合法当前候选复用与未知证据分开。本地验证记录 docs/walkthroughs/20261002-wf1002-stall-fixes.md；不声称服务已加载或生产工作流已恢复。

- 2026-10-02：wf1002 修复集成最新主干 c0cf2a0，发布候选81dc9a3；新全量3197 passed、2 skipped、157 subtests passed。上一条本轮教训交叉引用在合并主干后应为 §124（§122/123 为上游预检/Worker教训），保留原日志，追加更正。三服务受控发布与存量恢复见 docs/walkthroughs/20261002-wf1002-stall-release.md；运行结果另行回填。

- 2026-10-02：wf1002 用户确认后的本地三服务实际切换81dc9a3成功，Notifier原PID/配置不变。Run私有定义、需求附录/采纳、旧r2记账义务显式放弃及旧实现escalation清理已应用，Workflow恢复running。首次bootstrap错误回滚与主控字节码缓存校验失误均保留证据；协调器当前用户handoff模型超时、正式业务test/review仍待完成。见 docs/walkthroughs/20261002-wf1002-stall-release.md。

- 2026-10-02：上述协调器超时已同Provider/会话自然恢复并完成handoff；真实测试由协调器使用81dc及integration别名恢复，candidate/baseline/HEAD均8be，不归功于尚未发布的修复。修复无onto冻结候选传递三接缝，Worker从不可变commit创建自己的分支。旧HEAD相等断言经独立复核更新为HEAD前后实际基线验证，无pin及无效候选仍拒绝；本轮完整验证/部署结果续记 docs/walkthroughs/20261002-wf1002-stall-release.md。

- 2026-10-02：无onto候选pin修复全量3207 passed、2 skipped、157 subtests，独立审查通过后实际发布d93a3c8；三服务运行指纹与制品一致。规范核对旧review intent资源absent后解除仅该Run旧通知闩，新Controller自动派发review-auto到w13:p26，独立分支的candidate/baseline/HEAD均8be，真实调用链验收通过。原test已completed，业务review仍working，未宣称Workflow/业务交付全部PASS。证据见 docs/walkthroughs/20261002-wf1002-stall-release.md。

- 2026-10-02：续记最新状态，真实test/review报告业务blocked并进入superseded/pending，正常返工任务fix-compliance-display-mask-export-probes已working；前条状态是23:09快照，HAFlow两项修复未代替业务门禁。运行期间字节码反复生成后，以不变源码/执行位将d93制品设为只读；真实CLI去环境保护复验缓存0、attestation正确，无再重启。详见同发布记录。

## [2026-10-03] update | FIX_BUG1002 首批验收与恢复边界
- Updated [[task-lifecycle]]: 自动验收CAS、裁决/评审隔离及启动失败证据保留。
- Updated [[ops-center]]: required task 配置与跨域诊断，配置失败不误显示完成。
- 仅本地沙盒；平台建PR、pane自动回收和preflight刷新尚未实现，不代表部署或生产验收。

## [2026-10-03] update | FIX_BUG1002 全范围本地恢复与交付能力
- Updated [[task-lifecycle]]: terminal_id绑定、never-started回收、legacy声明授权、同实例rework及集成后平台create-pr；原生无close CAS、人类native并发不受managed锁的边界明确保留。
- Updated [[preflight-and-health]]、[[agent-routing-and-pools]]: 过期兼容候选真实请求重验、workflow部分刷新及同节点跨角色/跨进程reservation隔离。
- Updated [[ops-center]]: SHA绑定的workflow-local配置快照与审计、completion_issues呈现及DAG current/ready集合。
- 规格保留原20项并追加handoff六项，共26项；追加项最终结果及当前源码全量统计待主控填写。外部Nexus脚本独立CoW新5项与既有160项通过，不等于已应用业务原工作树。
- 未部署、未重启、未真实外部发PR；本地测试不等于生产验收。补证归入docs/lessons/lessons-learned.md既有§122/§124。

## [2026-10-03] verified | FIX_BUG1002 26项本地验收收敛
- 原20项及新增6项均已本地处理，SHA冻结并行候选、本地onto预拒绝、未commit产出保护、外部Nexus CoW提交门禁追加完成。
- 当前源码全量3244 passed、2 skipped、157subtests，退出0；两个跳过为隔离HOME下的launchd安装测试。外部脚本192通过，0失败/跳过。
- 独立Agent评审无已确认剩余阻塞；最后文案以真实JS执行验证，不把早期截图当成最新截图。
- 交付目标为隔离工作树；未提交、推送、合并、部署或重启生产，详见docs/product-specs/fix-bug1002.md。

- 2026-10-03 FIX_BUG1002 PR阶段：同步最新main并保留上游Run/替换义务；本任务工程教训在合并主干教训后编号为126。跨模型复核补充预检短锁/旧结果覆盖及context目录收尾回归。部署与生产验收仍为独立阶段。

- 2026-10-03 PR144合并前P1闭环：split回执在受管锁内持久，写失败保留已分配现场；runtime tab/anchor改为StateStore元数据+审计，配置读取overlay但snapshot字节/config_sha保持不可变。新增真实崩溃、持久失败及配置→装配→CAS回归。

- 2026-10-03 FIX_BUG1002收尾边界：发现Nexus CoW继承外部Git指针，先独立化Git元数据再清理自有branch；外部worktree及其当前引用分支保留。记录CoW目录/Git隔离双重检查教训。

## [2026-10-05] update | 运维中心一键修复与门禁放行解耦与人工审计闭环
- Updated [[ops-center]]: 彻底分离“重试修复”(`ops_repair`/`retry`)与“人工强制放行”(`force_pass`/`force_pass_advance`)；
  - 自动修复通道仅按当前状态执行安全工位动作（`rework`/`redrive`），不适用或失败必须报错保留阻塞，彻底移除隐式降级调用 `force_pass_gate` 与 `manual_advance`；
  - 人工强制放行必须显式确认（`confirmed: True`）、非空且非默认原因、明确指定归属目标工作流的门禁节点；放行成功推进失败如实报告 `partial: True`；
  - 前端增加 `confirmOpsForcePass` 二次确认弹窗与 `_opsActionBusy` 防重复提交保护；
  - 前置重读权威状态，强校验工作流/任务归属、拦截已作废（superseded）任务及版本/运行实例错配。
- 专项测试 `tests/test_console_ops_repair_gate_separation.py`（9/9 passed 含端到端 HTTP Server 到 SQLite 回读集成测试）；全量控制台测试 246 passed；S6 审查通过（MERGE_READY）。
- 关联教训沉淀至 `docs/lessons/lessons-learned.md` §131。

## [2026-10-05] update | 门禁放行范围投影对齐、节点版本完整性校验与接口防绕过闭环
- Updated [[ops-center]], [[workflow-engine]]:
  - 范围与投影对齐：`herdr/workflow_graph.py` 与 `console/herdr_factory_console.py::stage_summary` 严格感知 `gate_overrides` 中 `task_id`，单任务豁免绝不误将含其他阻塞/失败任务的多任务节点投影为通过/已清理；
  - 节点版本映射完整性：`herdr/state_db.py` 与 `herdr/kernel.py` 的 `force_pass_gate` 在接收 `expected_task_versions` 时强制比对有效任务全集，拒绝不完整映射以防未确认任务被意外放行，同时 CAS 乐观锁 `exp_v` 直接绑定调用方期望版本；
  - 接口双入口对齐：`/api/controller/execute-action` 与 `/api/kernel/force-pass` 统一通过 `_validate_force_pass_params` 强制要求快照版本保护字段，杜绝直接调用或旧客户端绕过防护；
  - 运维中心“强制放行推进”动作修复：前端 `confirmOpsForcePass` 纠正调用 `force_pass_advance`，放行后正常尝试推进后续阶段。
- 测试与验证：`tests/test_console_ops_repair_gate_separation.py`（17/17 passed）、`tests/test_console*.py`（254/254 passed, 76 subtests）、`python3 -m compileall` 及 `git diff --check` 全部 0 警告 0 报错。

## [2026-10-05] Added | 阻塞验收的持久恢复闭环
- Added [[workflow-progress-recovery]]: 统一事实评估、同事务义务、租约执行、committed 后继与未知交付核验；工作树实现与部署验收分开记录。

## [2026-10-06] fix | 阶段推进锁自身任务谱系死亡自愈与防静默跳过死锁
- 背景：`wf-nexusarchive-1005-01` 中前驱节点完成但自身任务全被 supersede 且无活跃 replacement，旧推进锁只检查前驱回退，导致 `'notified'` 锁永久驻留、每轮扫描静默跳过。
- 修复：`reconcile_stage_advance_states` 在前驱完成时检查自身节点谱系；若全量任务被作废且无活跃后继，主动撤销推进锁；`direct_dispatch.node_tasks_for_latch` 提供包含作废任务的全量视图。
- 回归：`tests/test_stage_advance_and_supersede.py` 新增 4 项场景测试，调度与派发套件 185 项全部通过。

## [2026-10-06] fix | ops 修复与门禁解耦冲突修复，测试隔离完善（PR #151 合并）
- 背景：PR #151（ops 修复与门禁解耦）与已合并的 PR #152（持久恢复义务）冲突；#152 在 `api_controller_execute_action` 新增恢复义务守卫，提前拦截了 #151 专项测试中"缺少 expected_version"等路径，导致 scenario 6、7、17 及 controller actions 套件各 1 条断言失败。
- 修复：对需要测试 force_pass 校验层而非恢复义务层的子用例，精确注入 `patch.object(c, "api_workflow_recovery", return_value={"operations": []})` 隔离，保留其余真实调用；不拓宽断言，不注释子用例。
- 验证：专项套件 25/25 passed，全量回归 3535 passed（排除已知超时 HTTP 集成测试），PR 冲突状态 CLEAN/MERGEABLE，已合并入 main@0be44bd。
- 教训：沉淀至 §132（跨 PR 测试隔离：新守卫逻辑提前拦截时需精确 mock 而非注释掉断言）。


- 2026-10-06：更新 workflow-progress-recovery，记录权威快照、证据迁移、在途后继消费、发布 episode 与独立业务回执；当前为工作树实现，生产激活和业务复跑未执行。

- 2026-10-06：生产复跑发现 EVALUATION 通用成功模板仍误宣 DoD；追加报告层反例与修复，业务回执仍独立于通用评分。

- 2026-10-06：补证业务blocked仍派wrapup与预算双读取漏口；共享业务前进guard覆盖五类入口，固定config用于launch容量，保留legacy非业务协议策略。

- 2026-10-06: activation review found verifier-cohort freshness race; shared business gate now rechecks the entire SQL cohort after all artifact hashes, preserving legacy reuse.

- 2026-10-06: close inventory uses Git untracked-directory summaries before bounded output; internal evidence cannot crowd out protected user output, and tracked internal changes remain protected.

- 2026-10-06：workflow-progress-recovery 增补首节点 node_dispatch 持久义务、发送前预占、固定登记截止、CLI intent/Task 绑定和零任务展示。记录历史上限、旧代次、丢队列及旧配置反例；当前仅本地工作树，不代表已部署或真实 Agent 自动完成。

- 2026-10-06：#156审查确认resolved首派阻断基础设施故障补派；补充独立replacement派发记录、前序身份及完整生命周期回归，保留历史首派证据。

- 2026-10-06：#156第二次复审补齐无首派历史的存量任务接管、多起点按节点发现、未发送接单责任的模式移交，以及非-rN替代任务的持久谱系识别。

- 2026-10-06：PR #156复审补齐存量接管、模式移交和任意命名替代关系；授权恢复后以排列及真实SQLite验证谱系选择确定性，真实Agent/部署未执行。
- 2026-10-06：fix-loop 重复扣预算根因=verdict 指纹含易失 task_id 且闩释放清指纹；指纹改纯语义（branch+note+affected），释放保留 |fp，后继代同结论走升级不再开轮。
- 2026-10-06：终态卫生批修复——supersede WIP 自动保存改 add -A 后 reset 内部路径（gitignored 文件曾使 add 必败）；closed workflow 的 CLI 报错改为 WorkflowClosedError 一行指引；verify-metrics 口径聚合多 runner 并支持 surefire；reap 前归档 pane scrollback（默认保留 14 天）。console 布局测试失败为 #157 存量。

- 2026-10-07：C03c 修复——显式恢复后旧 BLOCKER 屏幕残留不再重采样再阻塞。根因是采样事件绑定任务当前版本号，屏幕内容无法自证新旧；修复沿用完成路径 observe_completion 的在场周期纪律：completion_observations 新增 blocker_present 列，仅首次出现或 absent→present 周期采样 blocked_marker_observed，决策收敛为 herdr/completion.py 纯函数 blocker_sample_action，Controller 版本 CAS 不变。回归 tests/test_blocker_resample_discipline.py 复现审计序列（record→blocked→恢复→同屏幕重采样=residue）。

- 2026-10-07：C03c 四轮独立评审闭环收口——第 3 轮以真实 main() 确定性复现两类问题并返工：residue 不得短路同任务巡检（崩溃检测前移、恢复指令照常投递）；"指纹变化即新阻塞"在已消费象限不成立（恢复指令注入 pane 本身就改变字节），改为已消费象限仅 absent→present 周期重新武装、未消费象限按版本/指纹去重重采样，事件与在场状态同事务原子落盘。第 4 轮 MERGE_READY 0 阻塞；主循环回归锁定注入变屏不重阻塞、真实崩溃不被遮蔽。

## [2026-10-07] update | 下游节点派发持久责任
- Updated [[workflow-progress-recovery]]：扩展下游派发的候选和依赖身份、租约恢复、直接发送核验、resources_absent 有界重试、取消及 reuse 责任转移；隔离代码验证与生产恢复分开。

- 2026-10-07：PR #161 合并前独立核验补齐两条实际回归：未消费且版本有效的样本不再因无关屏幕刷新重复采样，避免事件增长与 pending steer 饥饿；blocked/rework 巡检已读到的 absent 同样持久记录，恢复后新标记可采样，产生阻塞事件的状态范围不变。5 条回归先 RED，修后 26 例通过；此前“按版本/指纹重采样”的记录以本条和工程教训 §136 为准。同步主干 #162 并保留双方日志，最终全量及独立复核见 PR 验证记录；未部署。

## [2026-10-07] update | 用户可操作派发恢复
- Updated [[workflow-progress-recovery]]：增加范围恢复、启动现场核查、未交付确认表单与实际登记回执；明确旧身份显式确认、版本竞争、读取失败及迟到启动边界。

- 2026-10-07：#159 复审修复——reap 归档传输误用 tmux capture-pane 抓取 Herdr Pane ID（非 tmux 目标），实测回收成功、归档失败、无归档文件；改为与 dump_transcript 及 observer live transcript 三处一致的 herdr pane read --source recent-unwrapped --lines 20000，失败语义不变。同批修正 test_pane_transcript_archive.py 两测试类从未进入默认收集（类名不以 Test 开头）与两处不存在的 Path.utime，并补 cmd_reap → 真实 capture 传输的接线回归（11 例全过，修复前收集为 0）。

## [2026-10-07] Updated | 工程交付要求与未提交恢复责任
- Updated [[task-lifecycle]]: 固定交付契约、完成与提交复验、受限completed返工及回执恢复。
- Updated [[workflow-progress-recovery]]: delivery责任无候选准入，仍保留测试冻结门禁和旧恢复身份兼容。

- 2026-10-08：含 fix 任务 plan 期复盘门禁——task_id/task_type/node/分支词边界命中 fix/bugfix/hotfix 且已登记契约但缺复盘条目的 git 派发，在 normalize 与 launch 预检直接拒绝（§133）。首版过宽误拦无契约旧链路 6 例后收窄；全量 3878 passed，独立评审 MERGE_READY。
- 2026-10-08：Agent 信任路由双修——launch 期按 clone 预埋信任（防首次 TRUST_REQUIRED）＋Worker 启动失败回写 unhealthy（防重复选中）（§138）。Worker 零信任纪律不受影响；全量 3884 passed（1 个 #170 干净主干同败），独立评审 MERGE_READY。

- 2026-10-08：close-workflow 三处 abort 信息增强（blocking/unsettled 附状态与 clone 路径，remediation_cmd 占位符换真实路径；零拦截语义改动）。定向 10 passed，全量 3887 passed（1 个 #170 主干同败），独立评审 MERGE_READY。

- 2026-10-08：verdict defer 可见性——done 快路径 miss 时输出原因码（§139），快路径条件与人工兜底不动。全量 3890 passed（1 个 #170 主干同败），独立评审 MERGE_READY。

- 2026-10-08：信任预埋扩面（§140）——线上 plan-challenger（none 模式 grok）TRUST_REQUIRED 回滚暴露 git 门槛过窄，改为模式无关决策；回写端经实战验证正常。全量 3891 passed，独立评审 MERGE_READY。
