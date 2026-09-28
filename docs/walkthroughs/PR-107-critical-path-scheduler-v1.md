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

## 6. 复审 P1 修复(第 2 轮)

四个 P1 全部命中"调度语义 vs 真实调用链"的裂缝,逐一修复并补回归:

### P1-1 默认 wrapup 绕过 Join Gate —— 已修

旧实现只认 `node_type == gate && deps >= 2`,而真实模板 `wrapup` 是 agent 节点,
于是 `test(A) pass + review(B) pass` 后 wrapup 照样 Ready。测试当时手工构造了
`node_type: gate`,掩盖了真实形态。

修复(`_scheduler_join_gate_allows`):两类形状都受门禁约束——
1. 显式 join 节点:`node_type == gate` 且依赖 >= 2;
2. **已被 Scheduler 接管**的 workflow(`latest_frozen_candidate_sha` 非空)中的
   任何多依赖节点(`wrapup` 即属此类)。
未接管的 workflow 保持 legacy passthrough —— 零行为变化。
选择"让多依赖 wrapup 拥有 join-before-dispatch 语义"而非把 wrapup 降级为纯 gate,
因为 wrapup 仍需 Agent 执行六步收尾。

回归:测试改为加载**真实模板**节点(`herdr.workflow.load_template`),断言
真实 wrapup 双分支异版本被拒、同版本放行,且未接管 workflow 仍 passthrough。

### P1-2 candidate_sha 不能证明验证版本 —— 已修(证据优先 + 严格相等)

两处修正:

1. **判据改为证据优先**(`herdr/scheduler.py`):拆分
   `extract_task_candidate_claim`(`candidate_sha`,调度器声明的意图)与
   `extract_task_verified_sha`(`baseline_commit`,worker 从真实 clone HEAD 记录的
   客观证据)。Join Gate 以**证据**为准,并新增 `join_evidence_mismatch`:
   声明与证据不一致一律拒绝汇聚。
2. **launch 侧严格相等**(`bin/herdr-task#_validate_test_delivery_baseline`):
   取消 `merge-base --is-ancestor` 放宽,只接受 `candidate_sha == baseline_commit`。
   同时校验 `--candidate-sha` 声明必须等于 delivery note 的 `candidate_sha`。
   空值一律 fail-closed(exit 2 + `test_baseline_rejected` 事件)。

回归:claim=A/evidence=B 的 join 拒绝;ancestor-only 基线被拒绝
(并断言不再依赖 ancestor 探测);空值 fail-closed。

### P1-3 branch fallback 与 launch preflight 打架 —— 已修(方案 A)

采用方案 A:`_scheduler_freeze_candidate` 冻结 SHA 后**同时保证 delivery note 存在**
(`_scheduler_ensure_delivery_note`,复用 `bin/herdr-task record_delivery_note`,
不建第二套交付真相):

- 无 delivery note → 记录provisional note(`{wf}-{node}-{sha[:12]}-review/test`),
  使 launch 的 FR-6.2 preflight 通过;
- 已有同 SHA note → 幂等返回 `exists`;
- 已有**不同** SHA note(候选轮换 / fix-loop 返工)→ 显式 `supersedes` 替换,
  沿用既有 replacement edge 校验;
- 补记失败 → freeze 返回 ""(上报 unprovable),join 侧 fail-closed。

provisional verifier 任务 id 带 short SHA 后缀:它们会成为 delivery alias,
两个候选共用 alias 会让 delivery 选择歧义(fail-closed),实测已踩到。

回归:真实执行 record-delivery 链路,断言 note 存在、幂等、轮换后
`select_effective_delivery` 唯一 tip,以及 SHA 不可证时返回空。

### P1-4 首次拒绝后闩被吞导致永久卡死 —— 已修(判定先于闩)

两条推进路径都改为 **join 判定先于一切闩**:

- sweep(`check_workflow_stage_advance`):join 拒绝 → `continue`,**不写** `queued`;
  冻结异常也不再落闩;
- direct(`try_direct_stage_advance`):join 拒绝 → 直接返回,不再调用
  `mark_stage_advance_notified`。

因此修正证据后下一轮 sweep 自动重估并恢复,不再需要 Controller 重启。

回归:sweep 路径断言 join 拒绝时 `mark_stage_advance_queued` 与 `coordinator_queue.put`
均未被调用;direct 路径断言 `mark_stage_advance_notified` 未调用、无 launch,
第二次(证据修好)正常派发。

### 未处理项(明确不在 #107 范围)

- **GitHub CI 证据缺口**:仓库当前无 `.github/workflows/`,PR 描述中的通过数是
  分支本地结果。建议单独小 PR 引入 CI(需先验证全量套件在 CI runner 上可移植),
  不塞进本 PR。
- **#106 post-merge Rollout 问题**:按指示单独 hotfix PR,不在 #107 内混合。

## 7. 复审 P1 修复(第 3 轮):两种事实生命周期分离

上一轮的 4 个 P1 已闭合。本轮两个新 P1 根因相同:

> **Candidate Frozen Fact 与 Final Delivery Record 是两种不同生命周期的事实。**
>
> - `candidate_frozen`(scheduler_facts)回答"这轮验证针对哪个 SHA";
> - `delivery record` 回答"最终由哪些**真实** Task 验证并形成交付"。

上一版在冻结时用占位 id 伪造了 delivery,把两者混成一条事实。已彻底移除。

### P1-A 冻结时写入假 verifier task id —— 已修(移除伪造)

旧实现生成 `<wf>-implementation-<sha>-review|test` 写进 delivery 的
`review_task`/`test_gate`。但 `delivery_record` 把这两个字段纳入**不可变
fingerprint**,于是真实任务创建后写回真 id 会命中 `[DELIVERY CONFLICT]`;
`check-delivery` 看到的 verifier 身份也是假的。

修复:`_scheduler_ensure_delivery_note` **整体删除**。`_scheduler_freeze_candidate`
现在只写 `candidate_frozen` 事实(携带 `source_node`/`delivery_branch`/
`rotated_from`),不产生任何 delivery note、不发明任何 verifier id。

回归:断言冻结后 `candidate_frozen` 存在、delivery note 数量为 0、
备注原文里不出现任何 `provisional` 字样。

### P1-B Fix-loop 新候选无法取代失效候选 —— 已修(冻结轨道独立)

旧的失败链:Candidate A → review blocked → A 失效 → 返工 → Candidate B →
冻结 B → 找不到 effective A → `supersedes` 为空 → `record_delivery_note` 抛
`[DELIVERY REPLACEMENT REQUIRED]` 并 `SystemExit(2)`(而 `except Exception`
抓不到 `SystemExit`)→ **第二轮 Test/Review 起不来**,直接打掉 #107 的核心场景。

修复分两层:

1. **冻结不再触碰 delivery**:候选轮换只体现为第二条 `candidate_frozen`
   事实(带 `rotated_from`),不再有 prior candidate 需要 supersede,
   因此不再触发 `SystemExit` 路径。
2. **launch 侧接受冻结候选作为可验证身份**(`bin/herdr-task`):
   - 无 delivery note → 仅当 `--candidate-sha` 与当前冻结 SHA **严格相等**才放行
     (`identity_source=frozen`),否则 `delivery_missing` fail-closed;
   - 有 delivery note 但**全部失效** → 同上按冻结候选放行,但**绝不复活**失效候选;
   - 有有效 delivery note → 仍以 record 为权威,claim 与 record 不一致即拒绝;
   - 最终仍由 `_validate_test_delivery_baseline` 强制
     `candidate_sha == baseline_commit`(严格相等,无 ancestor 放宽)。

回归(真实 `_preflight_delivery_identity` 调用链,隔离 docs/state):
匹配 claim 放行;缺 claim / claim 不匹配 / 无冻结事实 → exit 2;
失效候选 + 新冻结 B → 放行;失效候选 + claim 指向旧 A → 拒绝(不可复活);
存在有效 delivery record 时仍以 record 为权威;非 verifier 节点不受影响。

### 本轮结果

```
candidate_frozen 事实            ✅(不含 verifier 身份)
冻结阶段零 delivery note        ✅
provisional / 假 task id        ✅ 已彻底移除
fix-loop 候选轮换                ✅(A blocked → B 冻结 → Test(B)/Review(B) 可启动)
失效候选不可复活               ✅
严格 candidate_sha == baseline  ✅
```

证据:全量 `pytest -q` → **1985 passed, 50 subtests, 0 failed**;
`compileall` 与 `git diff --check` 干净。

### 仍未处理(不在 #107 范围)

- GitHub CI:仓库仍无 `.github/workflows/`,以上为分支本地证据,无 CI 独立确认。
- #106 post-merge Rollout 问题:单独 hotfix PR。
- 正式 delivery record 的写入仍由 review-pass/wrapup 依 FR-4.1 用真实 task id 完成
  (既有链路,本 PR 未改其语义)。
