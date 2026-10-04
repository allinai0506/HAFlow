# Handoff：Herdr Agent 候选分支系统性违反 dev 基线

> **状态**：待处理（交接给平台侧）
> **发现来源**：workflow `wf-project-1003-01`（项目：数凭 / NexusArchive），2026-10-04
> **严重度**：中高。**不阻断单次交付，但必然在流程末端引爆，且引爆点最贵**
> **是否影响其他项目**：是。任何使用「受管启动快照」+ 「dev 基线守卫」组合的项目都受影响

---

## 1. 一句话结论

Herdr 用 **workflow 启动时的 `base_sha` 快照** 创建 Agent 候选分支，而项目用 **`dev` 基线守卫**在**提交时**校验祖先关系。
当 `dev` 在 workflow 执行期间前进，快照即失效 → **每个** Agent 候选都系统性违反该守卫，
且**只有在流程最末端（wrapup 提交签署时）才暴露**，此时已付出全部双门禁成本。

---

## 2. 影响与成本（本轮实测）

| 项 | 值 |
|---|---|
| 暴露时点 | wrapup 阶段，`git commit` 被 pre-commit 拒绝 |
| 此时已消耗 | 2 轮完整 test 循环 + 1 轮 review + DEF-01..07 修复 |
| 直接后果 | review 授权签署**无法物化进版本控制** |
| 补救成本 | 合并 `dev` → 候选 SHA 变化 → **双门禁全部作废**，需完整重跑 |
| 二次伤害 | 合并导致后端文件行号漂移、文档引用失真，需额外事实性校正 |

**关键点**：失败不是发生在派发时，而是发生在**所有门禁都变绿之后**。这是最坏的位置。

---

## 3. 机制（时间线）

```
t0  workflow 创建，记录 base_sha = 0c888aa3e
    └─ 此刻 0c888aa3e 就是 dev ✅（已验证：它是 dev 的祖先）

t1  Agent 从 0c888aa3e 建候选分支开工
    └─ 此时仍然合规

t2  dev 独立前进（经人工 PR 合入，与本 workflow 无关）
    └─ dev: 0c888aa3e → 1c12f7f80(!1522) → a953d8f87(!1523)

t3  候选分支完工 → test / review 门禁全绿

t4  wrapup 尝试提交签署 → pre-commit 校验 dev 祖先 → ❌ 拒绝
    └─ merge-base(dev, candidate) = 0c888aa3e ≠ dev
```

**根因不是「快照错了」，而是「快照在 t0 正确、在 t4 失效」，而校验发生在 t4。**
这是快照式派发 + 末端校验的固有代价，不是配置疏漏。

---

## 4. 根因定位

### 4.1 守卫侧（项目约定，正确）

`scripts/git-hooks/enforce-dev-workflow.sh`：

```
所有非 dev / 非 main 的分支，提交与推送时必须以 dev（或 origin/dev）为祖先。
逃逸开关：ALLOW_NON_AGENT_BRANCH=1
锚定根：WORKTREE_ROOT=/Users/user/nexusarchive-worktrees
```

### 4.2 项目自带的合规路径（正确，但 Herdr 不用）

`scripts/new-dev-worktree.sh` 文件头注释即写着：

> `# Create a feature worktree from dev branch only.`

其流程：`git fetch origin dev` → `git switch dev` → `git pull --ff-only origin dev` → `git worktree add -b <branch> <path> dev`

**即项目设计意图明确：worktree 必须从 `dev` 建。** 这样必然满足 4.1 的守卫。

### 4.3 分歧点

**Herdr 的 clone 创建路径不走 `new-dev-worktree.sh`，而是以 workflow 的 `base_sha` 为基线。**

> ⚠️ **未钉死**：Herdr 中实际执行 clone/checkout 的具体调用点未定位到（`~/HAFlow/herdr/task_resources.py` 中
> `base_sha` 均用于 note 注解与 delivery 记录，非 clone 基线）。clone 创建逻辑应在 worker 侧另一模块。
> **这是接手方需要首先定位的位置。**

---

## 5. 证据（可复现）

### 5.1 分叉关系

```bash
git rev-parse dev                                    # a953d8f87
git merge-base dev <candidate>                      # 0c888aa3e  ← 仍是快照，非 dev
git merge-base --is-ancestor dev <candidate> && echo OK   # 无输出 = 违规
git log --oneline $(git merge-base dev <candidate>)..dev   # dev 侧独有的 2 个提交
```

### 5.2 **这不是孤例——本机 3/6 个 worktree 已违规**

```bash
for w in /Users/user/nexusarchive-worktrees/*/; do
  b=$(git -C "$w" rev-parse --abbrev-ref HEAD)
  git -C "$w" merge-base --is-ancestor dev HEAD && a=✓ || a=❌
  echo "$b $(git -C "$w" rev-parse --short HEAD) $a 落后$(git -C "$w" rev-list --count HEAD..dev)"
done
```

实测输出：

| worktree | HEAD | dev 祖先 | 落后 dev |
|---|---|---|---|
| `agent/codex/chore-retire-nexus-core` | `a953d8f87` | ✅ | 0 |
| `agent/gemini/fix-blob-family-residuals` | `e369bd92c` | ❌ | 2 |
| `agent/kimi-init` | `a953d8f87` | ✅ | 0 |
| `agent/opencode/fix-rule-unpublished-version-caliber` | `89f4acfb8` | ❌ | 1 |
| **`agent/qoder-init`** | **`0c888aa3e`** | ❌ | **2** |
| `agent/zcode/feat-xbrl-official-taxonomy` | `a953d8f87` | ✅ | 0 |

**`agent/qoder-init` 恰好卡在 `0c888aa3e`——与本 workflow 的受管启动快照是同一个 commit。**
`*-init` worktree 建一次就不管了，`dev` 一前进即静默失效，且**无任何告警**。

### 5.3 守卫确实会拦（不是纸面规则）

```bash
$ git commit --no-edit   # 在 agent/grok/docs-* 分支上
❌ [commit] 当前分支 agent/grok/docs-wf-project-1003-01-wrapup-auto 不满足 dev 基线约束。
   规则：所有本地开发分支必须从 dev 演进。
```

---

## 6. 方案对比

| # | 方案 | 改动面 | 能否根治 | 代价 / 风险 |
|---|---|---|---|---|
| **A** | **派发前 fail-fast**：dispatch 时即校验候选基线是否为 `dev` 祖先，不合规则拒绝派发并提示「dev 已前进，需 rebase 或指定新基线」 | Herdr dispatch 层 | ✅ 是 | 需定位 clone 基线来源；拒绝会让用户自己决定 rebase 时机，但**把失败从 t4 移到 t0** |
| **B** | **基线改用 `dev`**：clone/worktree 从 `dev` 建（对齐 `new-dev-worktree.sh`），`base_sha` 仅作审计元数据不再作 checkout 基线 | Herdr clone 创建 | ✅ 是 | 与「同一 workflow 多 Task 共用基线」的设计有张力——各 Task 起点会随时间漂移，跨 Task 比较基准不稳。**需先决策这一点** |
| **C** | **`*-init` worktree 定期重同步**：Controller 在每次 dispatch 前对 init worktree 做 `fetch + ff-only` | Controller 调度 | ⚠️ 部分 | 只修已有 worktree，不修「workflow 执行期间 dev 前进」的固有竞态 |
| **D** | **守卫改为 PR 时校验**：从 pre-commit 移到 pre-push / PR 创建时 | 项目钩子 | ❌ 否 | 只是把失败点后移，且会让 PR 带着违规历史提交。**不推荐单独使用** |
| **E** | **每次门禁前自动 rebase/merge `dev`** | Controller | ⚠️ 部分 | 会变更候选 SHA → **作废所有门禁**，与本轮遇到的问题同源。**不可行** |

### 推荐

**A（必做）+ C（补强）**，B 需独立决策后再动。

理由：
- A 把失败从「全绿之后」移到「派发之前」，成本从「两轮门禁 + 签署作废」降到「一次 rebase」。**这是本问题 90% 的价值。**
- C 消除 `*-init` 静默陈旧这个可观测性盲区。
- B 才是语义层面的根治，但会改变基线语义（见风险列），**不应与 A 捆绑决策**。
- E 与本轮暴露的问题同源（改 SHA → 门禁作废），**明确排除**。

---

## 7. 同轮发现的关联平台缺陷

以下 7 条同轮暴露，独立于本问题，但同属派发/回收链路，建议一并纳入平台侧 backlog：

| # | 缺陷 | 影响 | 严重度 |
|---|---|---|---|
| 1 | **候选无法重冻**：Scheduler 冻结 `bf46c22e` 后，合法的新候选被 FR-6.2 以 `delivery_missing` 拒绝，且无合法 CLI 入口重冻 | 候选变更后**无法在 workflow 内重跑门禁**，只能另开 workflow | **高** |
| 2 | **启动门禁失败仍写 `agent_started`**：门禁 raise 前即推进 intent phase，导致 `--confirm-agent-never-started` 被拒、`--apply` 空操作 | 任务无法重派，且**回收需操作者签署不实声明** | **高** |
| 3 | **CLI 误导提示**：Agent 健康失败时仍输出「Isolation is fail-closed. Re-launch with `--allow-reuse-implementation-agents`」，但真实判定是健康检查 | 诱导对**授权签字角色**使用 reuse opt-out，违反跨阶段隔离红线并留下不实审计 | **高** |
| 4 | **缺 `kind=sign-off` 枚举**：`note-add --kind sign-off` 被 CLI 枚举拒绝，授权签署只能以 `kind=decision` 落盘 | 授权记录与普通决策混同，**机器校验无法按 kind 过滤签署** | 中 |
| 5 | **基线校验晚于资源创建**：FR-6.2 校验在 clone + Pane 创建**之后**、Task 注册**之前**（`herdr-task:2555`） | 校验失败即留下孤儿 clone + 孤儿 Pane + 占用 Agent reservation，每次重试再累积一套 | 中 |
| 6 | **`DISPATCH DUPLICATE` 报错语义**：按 `(node, dispatch_role)` 判定，却输出**冲突的既有 task_id** 而非调用方 task_id | 误判为 task_id 冲突，进而去做错误的清理 | 低 |
| 7 | **`failed` 任务锁死 role 槽位**：Router 健康拒绝后仍写 `failed` 记录并占用节点累计容量 | 迫使后续派发不断更换 role 绕过；节点容量按累计数判定，`review` 节点已 `cumulative=3` vs `max=1` | 中 |

> 缺陷 #5 与本问题**同源**：都是「校验点与资源创建点的时序错配」。

---

## 8. 未钉死的点（诚实交代）

1. **Herdr clone 的实际基线来源代码位置未定位**。已排除 `task_resources.py`（其 `base_sha` 用于 note/delivery）。
   接手方需在 worker/clone 创建路径中定位 checkout 时使用的 ref。
2. **`base_sha` 是否可安全改为 `dev`** 取决于「同一 workflow 多 Task 是否必须共用静态基线」这一设计决策。
   本轮未找到该决策的书面依据。**建议先确认再动 B 方案。**
3. 本轮**未验证** `ALLOW_NON_AGENT_BRANCH=1` 与 `ALLOW_AI_WORKTREE=1` 是否被 CI 或远端另有约束。
4. 结论基于**单项目（NexusArchive）单轮**观测。5.2 的 3/6 违规率是本机快照，
   样本量不足以断言「所有项目都受影响」，但机制上可推广。

---

## 9. 复现步骤

```bash
# 1. 造一个陈旧基线
git checkout dev && git pull --ff-only
git switch -c agent/tmp/stale-test $(git rev-parse dev~1)   # 落后 dev 一个提交

# 2. 尝试提交 —— 应被拒
echo x > /tmp/stale-probe.txt && git add /tmp/stale-probe.txt
git commit -m "probe"
# 预期：❌ 不满足 dev 基线约束

# 3. 绕过（仅用于确认逃逸开关存在，不要用于生产）
ALLOW_NON_AGENT_BRANCH=1 git commit -m "probe"
git reset --hard HEAD~1
```

> 注：若在 `~/nexusarchive-worktrees/*` 之外操作，还会被 `check-ai-agent-worktree.sh` 拦
> （该钩子仅接受 `$WORKTREE_ROOT/*`，逃逸开关 `ALLOW_AI_WORKTREE=1`）。

---

## 10. 验收标准

方案 A 落地后应满足：

- [ ] 当候选基线不是 `dev` 祖先时，`herdr-task launch` 在**创建任何 clone / Pane 之前**即拒绝
- [ ] 拒绝信息明确指出「dev 已前进 N 个提交」并给出可选处置（rebase / 指定新基线）
- [ ] 该拒绝**不产生**孤儿 clone、孤儿 Pane 或 Agent reservation（对照缺陷 #5 的修复）
- [ ] `agent/qoder-init` 类 `*-init` worktree 在派发前被自动 ff-only 重同步，或其陈旧状态可被观测
- [ ] 新增回归测试：构造「`base_sha` 落后 `dev`」场景，断言派发被拒且资源零泄漏
- [ ] 方案 B（若采纳）：`base_sha` 语义变更需有书面决策记录，且不破坏跨 Task 基线一致性

---

## 附：本轮交付物现状（供接手方理解上下文）

- 交付 PR：Gitee **!1524**（`https://gitee.com/allinai888/dianzikuaijidangan/pulls/1524`）
- 候选 SHA：`1d9a091c4f88011de8769f92f44d31bcd25d049e`
- 分支：`agent/gemini/category-guard-dev-merge`
- 补救提交：`merge(dev)`（唯一冲突 `tech-debt-tracker.md`，按行取各自更优版本解决）
- **该 PR 当前不可合入**：`exec-plan` 签署栏仍 `pending` / `merge-blocked-until-signed=true`，
  须 review 节点在 `1d9a091c` 上重签；且该 SHA 未重跑门禁（缺陷 #1 使其在原 workflow 内无法重跑）
- 本 PR 正文已如实披露以上全部缺口