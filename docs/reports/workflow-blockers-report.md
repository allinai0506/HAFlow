# 历史技术债深度复盘与阻断根因治理报告 (Workflow Blockers Report)

> **公司**：上海共事智能科技有限公司 · 共事 · HAFlow  
> **定位**：让人和多个 AI Agent 一起把事情做完。*Human + Agent, in Flow.*  
> **基线核验**：覆盖 `wf-haflow-0923-02`、`wf-haflow-0924-01`、`wf-nexusarchive-1005-01`、`wf-project-1003-01` 四个关键业务与调度流水线  
> **日期**：2026-10-31  

---

## 0. 核心结论与治理总览

流水线中多次出现“明明单元测试全绿、执行结束后任务却停滞卡死、必须人工介入甚至重开流程”的现象，其本质是由**末端校验与时序错配**、**静默丢弃与假死循环**、**断言口径失真导致假绿**三大反模式叠加引起。

针对用户提出的 4 项关键技术债卡点（编号 7–10），本报告提供详细的代码级根因剖析、实操证据及闭环治理标准：

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              历史技术债阻断根因治理全景                                │
├────┬─────────────────────────┬───────────────────────────────┬─────────────────────────────┤
│ #  │ 工作流                  │ 阻断现象与特征                │ 核心技术债根因              │
├────┼─────────────────────────┼───────────────────────────────┼─────────────────────────────┤
│ 7  │ wf-haflow-0923-02       │ commit 归属 fail-open +       │ ① git_adoption 未核验 author │
│    │                         │ finalize 静默 return          │ ② candidate_blocked 无声空转│
├────┼─────────────────────────┼───────────────────────────────┼─────────────────────────────┤
│ 8  │ wf-haflow-0924-01       │ FR-1 state_db 死锁 +          │ ① 事务嵌套引发连接锁死      │
│    │                         │ FR-2 自动重推为 0 +           │ ② 易变字段比对致 episode 重置 │
│    │                         │ FR-6 审计绕过                 │ ③ 缺 opt-out 强制留痕阻断   │
├────┼─────────────────────────┼───────────────────────────────┼─────────────────────────────┤
│ 9  │ wf-nexusarchive-1005-01 │ SUPER_ADMIN 角色继承未生效     │ ① MethodSecurity 缺 Handler  │
│    │                         │ （D1/D2/D3 假绿）             │ ② 子串匹配/动词枚举产生假绿 │
├────┼─────────────────────────┼───────────────────────────────┼─────────────────────────────┤
│ 10 │ wf-project-1003-01      │ 门类守卫门禁注释断言 +        │ ① 测试注释虚构签署事实(DEF-02)│
│    │                         │ 基线盲区（DEF-02/R2/R3）      │ ② 快照派发后主干前进末端引爆│
└────┴─────────────────────────┴───────────────────────────────┴─────────────────────────────┘
```

---

## 1. 卡点 #7：commit 归属 fail-open 与 finalize 静默 return (`wf-haflow-0923-02`)

### 1.1 现象与现场还原
在工作流 `wf-haflow-0923-02`（Git 终化收编 HEAD，候选 `agent/opencode/fix-wf-haflow-0923-02-impl-fix2@33a1f1d`，base `c66fc46`）执行收尾时：
1. **他人提交被误收编 (fail-open)**：非该 Task 派发 Agent 产生的本地提交，只要时间戳在公差范围内、分支名称匹配且尚未推送到远端 `origin/*`，就被判定为 `attributable_commits` 并直接被 `ADOPT`，导致外部或污染代码混入交付候选。
2. **终化静默返回与停滞**：当遇到未经验证的集成结果（`integrate_result_unverified`）或候选被门禁标记阻断（`candidate_blocked`）时，`services/controller/git_finalize.py` 直接返回 `{retryable: False}`，既不调用 `_escalate_finalize` 升级人工，也不写入 Attention 状态，调度器每轮循环直接跳过，流程永久卡死。

### 1.2 源码级根因剖析

#### 根因 A：`git_adoption.py` 的作者身份空白（Fail-open）
在 `herdr/git_adoption.py:440-529`（`_classify_with_anchor`）中：
```python
# 现存判定链：
1. _check_merge(commits)           # 检查是否有 Merge Commit
2. _check_internal(commits)        # 检查是否包含内部路径 (.herdr 等)
3. _union_changed_paths(commits)   # 检查路径枚举
4. _check_remote_contained(...)    # 检查是否在 origin/* 已存在
5. _stale_commits(commits, cutoff) # 检查时间戳是否早于 cutoff
return ADOPT, {"reason": "attributable_commits", ...} # ⚠️ 判定通过！
```
- **漏洞点**：整个判定链**完全未读取或核验 Git Commit 的 Author / Committer 身份与邮箱**。只要第三方或并发进程向该工作区引入了提交，该提交就会被无条件收编为当前 Agent 任务的正规产出。

#### 根因 B：`git_finalize.py` 关键失败路径缺少升级（Silent return）
在 `services/controller/git_finalize.py`：
- **第 66–73 行**：
  ```python
  if task.get('integration_mode') == 'git' and task.get('commit'):
      failed_sha = task['commit']
      for verifier in ports._get_store().list_tasks(workflow_id=task.get('workflow_id')):
          if (verifier.get('status') != 'superseded' and not verifier.get('superseded_by')
                  and verifier.get('stage_verdict') == 'blocked'
                  and verifier.get('candidate_sha') == failed_sha):
              return {'retryable': False, 'kind': 'candidate_blocked'} # ⚠️ 静默退出，无事件无升级
  ```
- **第 380–385 行**：
  ```python
  confirmed_head = _settle_integration_result(ports, task, authorized_input, entry_identity)
  if not confirmed_head:
      return {"retryable": False, "kind": "refused", "reason": "integrate_result_unverified"} # ⚠️ 未调用 _escalate_finalize
  ```
  尽管文件注释明确声明“an unattributable result escalates to a human”，但代码直接 `return`，使得 `_check_finalize_retry` 在 `not retryable` 分支中认为已结算，从此不再调度该任务，导致工作流在完成前夜永久静默。

### 1.3 治理与修复方案
1. **身份锚定（Fail-closed）**：
   - 在 `git_interval_commits` 与 `git_log_commits` 中增加 `%an%x1f%ae` 解析；
   - 在 `classify_commit_state` 中增加 `expected_agent` / `allowed_authors` 校验，非受管 Agent 或外来邮箱提交触发 `REFUSED: foreign_author`。
2. **终化异常必留痕必升级**：
   - 对 `candidate_blocked` 触发 `ports._record_finalize_event(task, "finalize_candidate_blocked", ...)` 并调用 `ports._escalate_finalize(task, "candidate_blocked", ...)`；
   - 对 `integrate_result_unverified` 补齐 `ports._escalate_finalize` 调用，拒绝静默丢弃。

---

## 2. 卡点 #8：FR-1 state_db 死锁、FR-2 自动重推归零与 FR-6 审计绕过 (`wf-haflow-0924-01`)

### 2.1 现象与现场还原
在故障自愈规范（Fault Recovery FR-1..FR-6）实施过程（`wf-haflow-0924-01`）中：
1. **FR-1 state_db 锁冲突/死锁**：在完成状态 CAS（`compare_and_set_completion_transition`）高并发时，偶发 `sqlite3.OperationalError: database is locked`，导致状态无法写入；
2. **FR-2 自动重推计数常驻为 0**：任务处于 `blocked` 达到 SLA 阈值（默认 30 分钟）后，并未按照预期发送自动唤醒 prompt，后台日志显示每次巡检时 `repushes` 均为 0，但任务始终停滞；
3. **FR-6 路由隔离审计绕过**：在选择执行/验证 Agent 发生冲突时，通过特定路径降级或未配 reason 时，审计记录未被强制持久化便直接放行，击穿了跨阶段独立性红线。

### 2.2 源码级根因剖析

#### 根因 A (FR-1)：独占事务与连接未向下传递
- 历史实现中，外层函数开启了 `BEGIN IMMEDIATE`，但调用下游方法（如 `save_task` 或状态同步）时未传入活动的 `conn`，内部方法重新获取 `get_db_connection()` 开启平行连接竞争写锁，瞬间引爆 SQLite 锁超时。
- 必须严格遵守单一连接传递原则：`transition_task(..., conn=conn)`，且任何只读子查询在同一事务内执行，严禁无传递的嵌套写。

#### 根因 B (FR-2)：易变字段直比导致 Episode 被反复重置
在 `services/controller/blocked_claims.py:6-24`：
```python
def _blocked_episode_matches_task(ports, episode, task):
    same_entry = float(episode.get("entry_updated_at")) == float(task.get("updated_at"))
    same_version = int(episode.get("entry_version")) == int(task.get("version"))
    ...
    return same_entry and same_version and ...
```
- **缺陷链**：
  1. `entry_updated_at` 记录的是进入 blocked 时的时戳；
  2. 然而，后台 Sentinel、Liveness 探测或心跳巡检会对任务进行元数据轻微修正，导致 `task.updated_at` 前进；
  3. 下一轮扫描时，`_blocked_episode_matches_task` 判定 `same_entry` 为 `False`；
  4. 代码调用 `episode = _sla.new_episode(task, now)`，将 `episode["repushes"] = 0` 和 `episode["active_seconds"] = 0.0` **无情重置**！
  5. 结果：任务永远无法累计达到 1800 秒 SLA，重推计数永远被抹平为 0，陷入死锁。

#### 根因 C (FR-6)：隔离 Opt-out 校验缺乏原子强门禁
在 `herdr/agent_router.py:633-649`：
- 当请求指定 Agent 时，若 `allow_soft_degrade` 为真，直接将 `is_degraded = True` 标记并继续执行，而审计写入延迟到排名之后；
- 一旦后续出现异常或分支提前退出，使用非独立 Agent 的事件未能落入 `events` 审计表，形成特权绕过。

### 2.3 治理与修复方案
1. **FR-1 事务连接与 WAL 固化**：所有写状态方法强制要求 `conn` 可选注入，`compare_and_set_completion_transition` 内部的所有辅助写（包括事件落盘、观测删除）统一在同一个事务生命周期提交。
2. **FR-2 Episode 状态锚定**：
   - `_blocked_episode_matches_task` 不得比对瞬态的 `task.updated_at`，改用状态机跃迁历史中的 `entry_status_updated_at` 或以 `(task_id, run_id, blocked_status_timestamp)` 为稳定 Episode 键；
   - 杜绝因心跳或日志刷新导致的 Episode 重建与计数清零。
3. **FR-6 Fail-closed 强审计**：
   - 任何跨阶段复用（`stage_used_agents`）必须强校验 `allow_reuse_implementation_agents=true` 且 `reuse_reason` 非空；
   - 审计持久化必须在分配确认之前以同步事务写入，写入失败立即 raise `RouterIsolationRejection`，禁止带病运行。

---

## 3. 卡点 #9：SUPER_ADMIN 角色继承未生效与 D1/D2/D3 假绿 (`wf-nexusarchive-1005-01`)

### 3.1 现象与现场还原
在业务项目数凭全宗写接口授权工程（`wf-nexusarchive-1005-01`）中：
- 4248 项测试全量绿灯，但生产环境下超级管理员（持 `ROLE_SUPER_ADMIN`）访问核心写接口（如 `BasFondsController.deleteBatch`）时，**全部被 HTTP 403 拒绝**！
- 架构门禁测试（`BasFondsControllerWriteMethodSecurityGuardTest`）通过，但实际存在三重严重假绿：
  - **D1**：`SUPER_ADMIN` 继承自 `SYSTEM_ADMIN` 的层级声明在方法安全层完全失效；
  - **D2**：门禁对 `@PreAuthorize` 使用子串 `contains` 匹配，追加弱化权限依然跑绿；
  - **D3**：扫描入口仅匹配常用注解，遗漏通用 `@RequestMapping(method=POST)`。

### 3.2 源码级根因剖析

#### 根因 A (D1)：Spring Security 方法安全未注入 RoleHierarchy
在 Spring Security 架构中，Web 安全与方法安全（Method Security）使用的是**完全解耦的两个表达式解析器**：
- `SecurityConfig` 中虽然通过 `RoleHierarchyImpl` 声明了 `ROLE_SUPER_ADMIN > ROLE_SYSTEM_ADMIN`，并注入给了 `DefaultWebSecurityExpressionHandler`；
- 但控制器的 `@PreAuthorize("hasRole('SYSTEM_ADMIN')")` 是由 `MethodSecurityExpressionHandler` 负责求值的；
- 系统未显式声明提供 `static MethodSecurityExpressionHandler methodSecurityExpressionHandler(RoleHierarchy)` Bean，导致方法安全使用的是默认的、**无任何角色继承能力的空白解析器**；
- 表现：用户 URL 层面放行，进入 Controller 方法前被 AOP 拦截并抛出 `AccessDeniedException (403)`。

#### 根因 B (D2/D3)：门禁自身测试脆弱性（False Green）
1. **D2 子串断言缺陷**：
   ```java
   // 错误写法：
   assertTrue(expr.contains("hasAnyAuthority('fonds:manage')") && expr.contains("hasRole('SYSTEM_ADMIN')"));
   ```
   若攻击者或开发者手误追加 `or isAuthenticated()` 或 `or permitAll()`，由于原有子串依然存在，测试仍然通过！
2. **D3 动词扫描盲区**：
   测试仅通过 `method.isAnnotationPresent(PostMapping.class)` 等 4 个快捷注解扫描写入口；如果方法写成 `@RequestMapping(value="/delete", method=RequestMethod.POST)`，门禁直接漏检。
3. **测试替身假象**：
   历史上的 `RoleHierarchyTest` 直接 `new RoleHierarchyImpl()` 并断言其集合展开结果——这仅仅证明了数学逻辑，没有证明 Spring 容器将它接到了方法拦截链条上。

### 3.3 治理与修复方案
1. **方法安全表达式处理器注入**：
   在 `SecurityConfig.java` 明确提供静态 Bean，注入生产 `RoleHierarchy`：
   ```java
   @Bean
   static MethodSecurityExpressionHandler methodSecurityExpressionHandler(RoleHierarchy roleHierarchy) {
       DefaultMethodSecurityExpressionHandler handler = new DefaultMethodSecurityExpressionHandler();
       handler.setRoleHierarchy(roleHierarchy);
       return handler;
   }
   ```
2. **门禁重构为精确表达式守卫**：
   - 废除 `contains` 子串断言，改用 AST 或严格归一化字符串相等比较（必须恰好为指定表达式，词数与 `or` 计数严格吻合）；
   - 扫描写方法改为解析所有映射注解声明的 HTTP 动词（包含 `RequestMethod.POST/PUT/PATCH/DELETE`）；
   - 新增“门禁的门禁”（`BasFondsAuthorizeExpressionExactnessTest`），直接反向变异测试门禁本身的拦截准确度。

---

## 4. 卡点 #10：门类守卫门禁注释断言与基线盲区 (`wf-project-1003-01`)

### 4.1 现象与现场还原
在业务项目门类守卫重构流程（`wf-project-1003-01`）中：
1. **DEF-02 授权链造假**：`normalizeRowForDrawer.test.ts:88` 的测试注释声称“reviewer 签字见 exec-plan”，但实际 `exec-plan` 对应签署栏为 `pending / _______`。注释将“待满足的前置条件”虚构为“已满足的事实”，逃避了真实的授权闭环。
2. **基线漂移末端引爆（最昂贵失败）**：
   - Agent 经历 2 轮完整测试循环 + 1 轮完整 review，全部门禁变绿；
   - 在流程最后一步 `wrapup` 提交签署时，本地 pre-commit 守卫（`enforce-dev-workflow.sh`）触发：
     ```bash
     ❌ [commit] 当前分支 agent/... 不满足 dev 基线约束。
        规则：所有本地开发分支必须从 dev 演进。
     ```
   - 此时为了合入 `dev`，必须重新 rebase/merge，导致候选 SHA 彻底变化，**此前付出的双门禁与测试凭据全部瞬间作废，必须从头完整重跑**。

### 4.2 源码级根因剖析

#### 根因 A (DEF-02)：形式主义注释替代机器校验
测试用例中将需要严格审计的行为扩张（别名归一化），通过一行非执行的测试注释敷衍了事。缺少针对 `exec-plan.md` 签署状态的机器可读检查，导致未授权改动直接溜过前置检查。

#### 根因 B (基线盲区 R2/R3)：快照派发与末端守卫的时序断层
- **时钟 t0**：Herdr 创建 workflow，以当时的快照 SHA（如 `0c888aa3e`）作为 `base_sha`；
- **时钟 t1**：Agent 基于该快照创建 worktree/clone 开始编码；
- **时钟 t2**：主干 `dev` 发生独立前进（其他 PR 合入）；
- **时钟 t3**：Agent 完工，测试/评审门禁在快照基线上全绿；
- **时钟 t4 (末端引爆)**：准备提交交付时，项目级守卫在 pre-commit 检查 `merge-base(dev, HEAD) == dev`，发现快照落后，直接拒交！

**根因**：Herdr 派发阶段（t0）存在**基线盲区**，未在任务派发前执行基线新鲜度预检；而项目守卫又设置在流程最末端（t4），形成了最昂贵的失败路径。

### 4.3 治理与修复方案
1. **DEF-02 显式状态门禁**：
   - 废除模糊散文注释，在 `exec-plan` 签字栏下固化机器可读标记：`SIGN-OFF-STATUS: signed [reviewer_id: SHA]`；
   - 门禁脚本增加正向 grep 扫描，若处于 `pending` 或格式不符，测试强制失败。
2. **基线盲区治理：派发前 Fail-fast (方案 A + C)**：
   - **方案 A（必须）**：在 `herdr-task launch` 执行前置检查。在创建任何 clone 或 Pane 之前，先运行 `git merge-base --is-ancestor dev <base_sha>`；若 `dev` 已前进，立即拒绝派发并给出可操作处置（`需先 rebase dev`），将失败成本从 3 小时降为 0 秒；
   - **方案 C（补强）**：针对常用的 `*-init` 基准工位，Controller 派发前自动执行 `fetch origin dev && pull --ff-only`，消除陈旧 worktree 的静默盲区。

---

## 5. 验收标准与防复发门禁清单

针对以上 4 项技术债的治理与长期防护，设立以下验收标准：

| 验收维度 | 阻断卡点 | 防复发自动化校验项 | 验收命令 / 预期结果 |
|---|---|---|---|
| **Git 终化收编** | #7 | 拦截他人提交；禁止任何无状态变更的静默 return | `pytest -q tests/test_git_adoption.py tests/test_finalize_empty.py` 全部通过；无未升级的退出路径 |
| **故障自愈状态机** | #8 | 事务单连接传递；Episode 跨扫描保真；路由 Opt-out 强审计 | `pytest -q tests/test_impl_fix4_blocker_regression.py` 全部通过；模拟更新时不重置 repush 计数 |
| **方法安全与门禁** | #9 | 注入 `MethodSecurityExpressionHandler`；Guard 精确匹配与动词全扫描 | `BasFondsAuthorizeExpressionExactnessTest` 与端点矩阵通过；变异测试全部被捕获 |
| **基线与签署规范** | #10 | 派发前基线祖先核验；机器可读签署标记扫描 | `herdr-task launch` 在落后基线下 0 秒 Fail-fast；未签署文件被 CI 门禁直接拦截 |

---

> **交付记录**：本文档已固化至系统知识库，相关规范约束同步映射至 `docs/engineering/agent-engineering-protocol.md` 与 `RULES.md`。
