# PR #107 — Critical-Path Scheduler v1（test ∥ review 并行 + 冻结候选 + Join Gate）

> 状态：实现中（CoW 沙盒 `~/.sandboxes/cpscheduler-v1`，分支 `feat/critical-path-scheduler-v1`）
> 基线：`main@a957663` 全量 `1927 passed, 50 subtests passed`（0 失败）

## 0. 问题定义

`software-development-v1` 的 `review.depends_on=[test]` 把测试与评审钉死为串行，
关键路径白白多出一个评审周期。直接把依赖改平（`review.depends_on=[implementation]`）
就能并行，但会引入三个正确性黑洞：两条分支可能各验证各的版本（test 测 A、review
审 B）；实现分支在验证期间被二次提交会让旧结论悄悄变成过期结论；wrapup 无法以
确定性方式证明"两条分支在同一版本上都通过了"。

本 PR 只做一件事：让 `test ∥ review` 并行，同时用不可变 `candidate_sha` +
确定性 Join Gate 把上述黑洞全部堵死。借用《让子弹飞》的台词——**并行可以发，
子弹（候选版本）必须先冻结，汇聚必须验弹壳**。

## 1. 方案总览（最小改动面）

| 文件 | 改动 | 行数 |
|---|---|---|
| `herdr/scheduler.py`（新） | 纯函数核心：ready 计算 / join 判定 / SHA 提取 / 并行度量 | ~350 |
| `herdr/scheduler_facts.py`（新） | 审计证据：`scheduler_decision` / `candidate_frozen` / `join_gate_verdict` 事件 | ~130 |
| `herdr/direct_dispatch.py` | spec/prompt 透传 `candidate_sha`（可选关键字，默认空=原语义） | +16 |
| `bin/herdr-task` | `launch --candidate-sha` 持久化到任务记录（只写字段） | +10 |
| `services/herdr-controller.py` | 期望候选解析 / 冻结 / join 接线（scheduler 缺失时退回原语义） | +172 |
| `workflow_templates/software-development-v1.yaml` | `review.depends_on: [test]`→`[implementation]`；`wrapup`→`[test, review]` | +7/-3 |
| `tests/test_scheduler_v1.py`（新） | 纯函数 18 例（§31 场景 1/2/4/5/6/7/8/12） | — |
| `tests/test_scheduler_facts.py`（新） | 幂等冻结 + 审计读写 5 例 | — |
| `tests/test_scheduler_dispatch_e2e.py`（新） | 接线 9 例（场景 3/9/10/11） | — |

设计纪律：
- **零新表、零新枚举**：候选冻结与判定审计全部走既有 `events` 表；
  任务绑定复用 `payload_json` 自由字段（与 `branch`/`onto_branch` 同机制）。
- **delivery_record 是唯一交付身份源**：期望候选优先读 delivery note，
  调度器不建第二套真相（`select_effective_delivery` → `candidate_sha`）。
- **Join Gate 零 LLM**：`evaluate_join_gate` 纯函数，失败一律 Fail-Closed。
- **旧模板零行为变化**：`candidate_sha` 为空时 planner/controller 走原语义；
  非 join 节点（`node_type != gate` 或依赖 < 2）直接放行。

## 2. 关键语义

- **冻结**：`implementation` 完成后冻结候选 SHA（delivery note 优先，
  依赖分支 HEAD 兜底，有界 git 调用，失败留空→门禁侧 fail-closed）。
  同 SHA 重复冻结幂等（`exists`），换 SHA 记录轮换（`rotated_from`）。
- **绑定**：`test`/`review` 派发时把冻结 SHA 注入 prompt（Agent 可见声明）
  与 `--candidate-sha`（任务记录持久化，供门禁机读）。
- **汇聚**：`wrapup`（多前置汇聚点）放行当且仅当全部前置完成、
  verdict 全 pass、SHA 非空一致、且等于当前冻结候选。
  拒绝原因六选一：`waiting / blocked / missing_candidate /
  mismatch / stale / satisfied`，每次判定落 `join_gate_verdict` 事件。
- **并行证据**：`parallel_section_metrics` 用任务自带时间戳计算分支
  活跃窗口重叠秒数；缺时间戳标记 `unknown`，不猜测。

## 3. 测试矩阵（映射 PRD §31）

| 场景 | 用例 | 状态 |
|---|---|---|
| 1 并行同时就绪 | `test_parallel_branches_ready_together` | ✅ |
| 2 旧模板串行保持 | `test_legacy_sequential_semantics_preserved` | ✅ |
| 3 真实重叠证据 | launch 双发同 SHA + `parallel_section_metrics` | ✅ |
| 4 Join PASS | `test_join_gate_passes_on_same_sha` | ✅ |
| 5 Join FAIL（blocked/SHA 冲突） | blocked + mismatch 两例 | ✅ |
| 6 candidate 变更→stale | `test_join_gate_stale_on_candidate_advance` + 轮换记录 | ✅ |
| 7 test A / review B 拒绝 | `test_join_gate_refuses_divergent_branches` | ✅ |
| 8 running 排除 | `test_running_nodes_excluded_from_ready` | ✅ |
| 9 崩溃恢复 | join waiting 保持 + completed 不丢失 | ✅ |
| 10 幂等 | `mark_stage_advance_queued` 既有闩 + 冻结幂等 | ✅ |
| 11 资源约束 | vacuous/失败 fallback（沿用既有路径） | ✅ |
| 12 确定性重放 | `test_deterministic_replay_same_inputs` | ✅ |

## 4. 验收证据

- 新增 32 例：`pytest tests/test_scheduler_v1.py tests/test_scheduler_facts.py
  tests/test_scheduler_dispatch_e2e.py` → 全绿。
- 相关既有：`test_direct_stage_dispatch / test_dispatch_candidate /
  test_workflow_engine / test_software_development_v1_template /
  test_dynamic_workflow_schema` → 全绿。
- 全量回归：`pytest -q`（后台运行，日志 `/tmp/haflow_pr107_full.log`）。
- 语法：`compileall` + `git diff --check`（待收尾执行）。

## 5. 已知边界与后续

- 冻结依赖分支 HEAD 可达：`implementation` 任务无分支信息的极端情形下
  期望候选为空，join 门禁 fail-closed（拒绝推进，需人工/总指挥介入）。
  这是 Fail-Closed 设计的有意保守，不是静默放行。
- `wrapup` 本身仍是 agent 节点（非 `node_type: gate`），当前以后置依赖
  `[test, review]` 做 DAG 级汇聚 + join 判定做语义级汇聚；若未来模板引入
  显式 `node_type: gate` 的 join 节点，接线已预留（`_scheduler_join_gate_allows`
  按 `node_type==gate && len(deps)>=2` 识别）。
- 并行窗口度量依赖任务时间戳字段；历史任务缺字段时标记 `unknown`。
