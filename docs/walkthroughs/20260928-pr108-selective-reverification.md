# PR #108 — Selective Reverification v1（候选轮换后选择性跳过重复验证）

> 归档自本 PR 的三份门禁工件（Entry Gate / S5 验证证据 / S6 独立评审）。
> 原始工件在 `.omc/` 内且被 `.gitignore` 忽略，不随仓库留存；本文件是它们的长期副本。
> PR: https://github.com/allinai0506/HAFlow/pull/108 · 合并提交 `f220e46`

---

## 0. 问题定义

#107（Critical-Path Scheduler v1）确立了正确的不变量：候选 SHA 被冻结后，Join Gate
要求 test / review 都证明**同一个** SHA。但它对「变化」的反应是最保守的策略——
fix-loop 返工时无条件作废 `test` 任务，哪怕它已经写下 `verdict=pass` 且
`verified_candidate_sha == A`。

原因在当时是充分的：没有任何**机器可判定**的办法表达「这个改动影响不到这个
verifier」。代价是一次只发现「README 少了一段说明」的评审，也会让整个测试套件
重跑一遍。

#108 要解决的是：**候选改变后，哪些 verifier 必须重跑，哪些 PASS 可以安全复用。**
答案是「只有能被证明的才能复用」，而不是「让 LLM 判断这次改动应该不用重测」。

---

## 1. 第一原则：Reuse must be proven. Rerun is the default.

`UNKNOWN → RERUN`，不是 `UNKNOWN → REUSE`。

复用只在下列四者**同时**成立时允许，任一无法证明即 RERUN：

1. **真实 git diff** —— `git diff --name-status -z A B`，不是 Agent 自述
2. **显式非影响范围** —— 显式声明的「明确不会影响」的路径
3. **带双重绑定的来源 PASS** —— `source.candidate_sha == A` **且**
   `source.verified_candidate_sha == A` **且** `verdict == pass`
4. **不可变派生事实** —— 绑定到 candidate 冻结 episode 的 `reverification_decision`，
   绝不改写历史 Task

全程不使用 LLM，不做 import / AST / CodeGraph 依赖推理，不做测试用例选择。

---

## 2. 方案总览（最小改动面）

```
Candidate Frozen Fact
        ↓
Reverification Planner      herdr/reverification.py（纯决策核心）
        ↓  reuse / rerun
Scheduler                   herdr/scheduler.py
        ↓
Router → Worker
        ↓
Join Gate                   同一个 resolve_effective_verification
```

| 文件 | 职责 |
|---|---|
| `herdr/reverification.py` **(新, 713 行)** | git diff 解析、祖先检查、glob 范围匹配、影响矩阵、计划构建、决策身份、策略指纹、指标 |
| `herdr/scheduler_facts.py` (+229) | `reverification_decision` 事件（复用已有 events 表，**无新表**）；`find_reuse_fact` 四重精确绑定 |
| `herdr/state_db.py` | `record_event_if_absent`：`BEGIN IMMEDIATE` 写锁下的原子 check+insert |
| `herdr/scheduler.py` (+180) | `resolve_effective_verification`；门禁新增可选参数 `reuse_facts` |
| `services/herdr-controller.py` (+501) | 轮换出计划、复用不派发、复用计入完成、无候选时不闩 |
| `bin/herdr-task` (+155) | 只读审计 CLI `reverification status\|history` |
| `workflow_templates/software-development-v1.yaml` (+20) | 策略声明 |

**未修改**（#107 契约全部保持）：`verified_candidate_sha` 写入路径、
`baseline_commit`、`candidate_sha` claim 路径、Agent Router、Canary、Rollout、
stage latch 语义。

---

## 3. 关键语义

### 3.1 策略按「明确不会影响」建模

不是「哪些文件要重测」——那种建模下，新增目录会因「没配置」被误判为安全。

| verifier | 安全范围 | 理由 |
|---|---|---|
| `test` | `docs/**/*.md` | 运行时读取的 markdown 只有 `.herdr-loop/*.md` 与根目录 `AGENTS.md` / `CLAUDE.md` / `RULES.md`，全部在范围外 |
| `review` | `[]` | v1 永远重跑。`[]` 表示**不存在**安全复用范围，不是「什么都能复用」 |

### 3.2 策略身份是指纹，不是版本号

`version` 是人类标签，收窄范围**不会**改变它。以版本号为键，收窄策略就纯属装饰。
因此策略身份 = **已解析策略的指纹**（`herdr/reverification.py#policy_identity`：
版本 + 每个 verifier 的范围），进入 `decision_identity` 与 `find_reuse_fact` 查表，
收窄或删除配置会真正撤销既有复用。

### 3.3 事实绑定 candidate episode，而非 SHA

**回滚会重新冻结一个曾经冻结过的 SHA。** 只认 SHA 时，上一轮的 reuse 会复活，
把一个从未验证过的候选判为已覆盖。因此事实绑定
`candidate_frozen_event_id`（冻结事件 id，而非 SHA）；后续轮次重复出现的同一
`(from, to)` 因此是独立 episode。

### 3.4 复用是调度决策

复用 verifier **不创建 Task**。因为它没有任务，「节点完成」与 Join Gate 都改读
同一个纯函数 `resolve_effective_verification`——台账与门禁不可能对同一分支给出
相反答案。

优先级：`fresh B verification > reuse→B fact > nothing`。分支上存在任何活跃任务时，
复用事实完全不参与判定。

### 3.5 非线性候选一律 RERUN

`A` 不是 `B` 的祖先（force push / 切分支 / 回滚到分叉历史）时不做任何推断。

### 3.6 Rename 两侧都判

`R herdr/foo.py docs/foo.md` 不得因为目标落在 `docs/` 就放行。

---

## 4. 验收证据

### 4.1 自动化验证

```
新增专项        172 passed
全量            2205 passed, 50 subtests, 0 failed
compileall      rc 0
ast.parse       rc 0（3 个无扩展名脚本）
git diff --check rc 0
```

**#107 兼容性是实测的**：`evaluate_join_gate(reuse_facts=None)` 与 `3a84659`
实现做了 20 万组随机差分 → **0 处判决不一致**。

**隔离性**：`~/.herdr-controller/stage-state.json` 全量测试前后 sha1 一致
（`b1f02b4f063d1c34`），无 `wf-rever*` 残留键。

### 4.2 真实链路

`tests/test_reverification_controller.py` 驱动真实 `check_workflow_stage_advance`
sweep + 真实 git 仓库 + 真实 stage latch（只拦截 `subprocess.run` 的 launch argv）。

docs-only 轮换下的实际输出：

```
[STAGE ADVANCE QUEUED]  workflow=wf-rever-ctl implementation -> test
[STAGE ADVANCE QUEUED]  workflow=wf-rever-ctl implementation -> review
[STAGE ADVANCED DIRECT] workflow=wf-rever-ctl node=test   tasks=wf-rever-ctl-test-auto
[STAGE ADVANCED DIRECT] workflow=wf-rever-ctl node=review tasks=wf-rever-ctl-review-auto
[STAGE REVOKE]          workflow=wf-rever-ctl node=test: predecessors no longer complete, revoking 'notified' lock
[STAGE REVOKE]          workflow=wf-rever-ctl node=review: predecessors no longer complete, revoking 'notified' lock
[STAGE ADVANCE QUEUED]  workflow=wf-rever-ctl plan -> implementation
[REVERIFICATION RERUN]  workflow=wf-rever-ctl node=review 76403e6f -> 68bac932 reason=no_reusable_scope_declared changed=docs/user-guide.md
[REVERIFICATION REUSE]  workflow=wf-rever-ctl node=test   76403e6f -> 68bac932 reason=all_changed_paths_explicitly_non_impacting changed=docs/user-guide.md
[STAGE ADVANCE QUEUED]  workflow=wf-rever-ctl implementation -> review
[STAGE ADVANCED DIRECT] workflow=wf-rever-ctl node=review tasks=wf-rever-ctl-review-auto
```

前四行是候选 A 的**引导轮**（test 与 review 确实被派发）；最后三行是候选 B 的
**轮换 sweep**：复用节点不产生任何 launch，只有 review 被派发。

### 4.3 可观测性

```bash
herdr-task reverification status --workflow-id <wf>
```

```
workflow=<wf> candidate=68bac932 verifiers=2 reuse=1 rerun=1 reuse_rate=0.50
- test: reuse (76403e6f -> 68bac932)
    reason: all_changed_paths_explicitly_non_impacting
    changed: docs/user-guide.md
    scope: docs/**/*.md
    source: wf-rever-ctl-test-auto verdict=pass verified_candidate_sha=76403e6f
    policy: selective-reverification-v1
- review: rerun (76403e6f -> 68bac932)
    reason: no_reusable_scope_declared
    changed: docs/user-guide.md
    policy: selective-reverification-v1
```

---

## 5. 评审闭环：四轮，20 项缺陷

### 5.1 第 1 轮（内部对抗评审）

| 级别 | 缺陷 |
|---|---|
| P1 | 复用来源走 `extract_task_verified_sha`，把 `baseline_commit` 当成完成证据 |
| P1 | 排除 superseded 来源 → fix-loop 返工时永远没有来源，功能在真实返工下永不生效 |
| P1 | 候选轮换对处理它的那一轮 sweep 不可见 → 被旧事实满足的节点本轮仍算完成，C 候选的 verifier 从未运行就被跳过 |
| P1 | 冻结上提后 `if deferred: return` 早于 `is_workflow_completed` → 全部完成的 workflow 永远关不掉 |
| P2 | 计划每 2s 重新推导一次，成本约 2.8 倍 |
| P2 | `is_node_complete` 在 `scheduler_core is None` 时抛 `AttributeError` |
| P2 | 台账与门禁用两套「满足」规则 → 可能永久等待 |
| P2 | 事实丢弃 `reusable_scope`，无法自解释 |
| P2 | 热路径每节点重解析 YAML |
| P2 | 只读 CLI 会在空环境创建 500KB 数据库 |
| P2 | 信任调用方传入的 `decision_identity` |
| P3 | 若干文档与注释与代码不符 |

### 5.2 第 2 轮

| 级别 | 缺陷 |
|---|---|
| P1 | `policy_version` 无法撤销收窄的范围，**而注释恰好宣称防住了这件事** |
| P2 | 缺字段默认「fresh」是 fail-*open* |
| P2 | `find_reuse_fact(policy_version="")` 关闭了策略过滤 |
| P2 | 控制器测试写穿到用户真实的 `stage-state.json`（`STAGE_STATE_FILE` 在 import 期被读成模块常量，env patch 无效） |
| P2 | `test_reverification_e2e.py` 声称 E2E 但全程不碰控制器 |
| P3 | 工件中的测试计数、不可复现的日志、错误的因果说明 |

### 5.3 第 3 轮

无 P1/P2 安全或正确性缺陷。剩余为成本与文档项。

### 5.4 第 4 轮（外部评审 PR）

| 级别 | 缺陷 |
|---|---|
| P1 | **reuse 事实未绑定 candidate episode**。回滚会重新冻结曾冻结过的 SHA，SHA 键查找让旧轮次的 reuse 复活，`test(B)` 从未派发。原有「A→B→A」测试用的是三个不同 SHA，从未覆盖回到完全相同 SHA 的场景 |
| P1 | **来源未校验 candidate claim**。§14 要求 claim 与 evidence 都绑定 A，但只校验了后者 |
| P2 | **并发写非幂等**。8 个并发写者产生 8 行重复 |

### 5.5 变异验证

每项修复单独回退，确认对应测试变红——证明测试在守门而非恒真：

| 回退的修复 | 结果 |
|---|---|
| 从查表移除 episode 绑定 | `test_rollback_to_a_previous_candidate_cannot_resurrect_old_reuse` FAILED |
| 移除 source claim 校验 | 2 个测试 FAILED |
| 原子写退回 read-then-compare | `test_concurrent_writers_produce_exactly_one_fact` FAILED，**8 条重复行**而非 1 条 |

---

## 6. 已知边界

- `docs/**/*.md` 是**声明的运维策略**，不是「文档不可能影响测试」的证明。收窄依据是
  grep 实测的仓库事实（哪些 markdown 被运行时读取），`TemplatePolicyTest` 钉住这条边界。
- 绝对 sweep 开销未由本方实测。第三方量测 6 节点 2.03×、8/12 节点更快；本方无法
  复现其 harness，因此只以调用点检查支撑结论，不把他人数字当作本方测量。
- 一次瞬时 diff 失败会被记为永久 RERUN，该轮复用被没收——方向安全。
- 复用单跳，不递归。
- 仓库无 GitHub Actions / commit status，`2205 passed` 属本地分支证据，非 CI 强制结果。

---

## 7. 后续

- **Selective Replan**（真正的选择性重规划）须开独立 PR。#108 只做重新验证决策，
  不做 DAG 改写、不做实现任务重规划。
- 若要扩大安全范围，须先在仓库中证明该路径不被运行时读取，再加入策略——不得为了
  演示功能把大量目录塞进 safe list。

---

## 8. 关联

- 教训沉淀：`docs/lessons/lessons-learned.md` §94
- Wiki：`wiki/dag-workflow-engine.md` §5、`wiki/log.md`
- 前序：PR #107 Critical-Path Scheduler v1
  （`docs/walkthroughs/PR-107-critical-path-scheduler-v1.md`）
- 既有同族教训：§93（身份三字段不可顶替）、§91（测试会写穿实盘注册表）、
  §92（只读不等于无副作用）
