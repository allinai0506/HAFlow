# Handoff：workflow `wf-project-1002-01` 期间发现的 HAFlow 问题清单

> 报告时间：2026-10-02
> 触发场景：NexusArchive「合规报告请求竞态 + JSON 导出假成功」工作流全流程（requirements → plan → implementation）
> 涉及 release：`22fb1a9d` → `c0fdee6c` → `c0cf2a00`（当前）
> 代码基线：`~/HAFlow` @ `c0cf2a0`（`main`）
> 本文档只做记录与建议，**未修改任何 HAFlow 代码**（唯一例外见 §4，已修复并部署）

---

## 0. 摘要

本次 workflow 跨越 3 个节点、经历 2 次服务器重启、1 次派发被中断，最终 implementation节点交付成功（`integrated` + `verdict=pass`，PR #1514）。过程中暴露 **19 个 HAFlow 问题**，其中 4 个为阻断级。

其中最值得注意的不是单个 bug，而是**两条协议层面的死结**（§1.2、§3.1）：平台约定「收尾节点分支不推送」，而任务需求要求「Agent 创建 PR 并保持待审查」，而 Herdr 本身**不具备建 PR 能力**。这三者无法同时满足，任何单点修复都绕不开。

---

## 分级速览

| # | 级别 | 问题 | 位置 |
|---|------|------|------|
| 1.1 | P0 | Agent 自建分支导致终化拒绝，且 `commits=0` 误导 | `git_adoption.py` |
| 1.2 | P0 | Agent 提前 push + 自建 PR 导致终化拒绝（与需求冲突） | `git_adoption.py:260` |
| 1.3 | P0 | `required_task_ids` 跨 workflow → 节点永久无法完成 | `scheduler.py:138-155` |
| 1.4 | P0 | `[AUTO ACCEPT]` 绕过验收门禁，`stage_verdict` 空放行 | `herdr-controller.py` |
| 2.1 | P1 | Supervisor 判 `work_off_track` 却仍放行完成 | `herdr-controller.py` |
| 2.2 | P1 | launch 失败后 pane 泄漏 | `herdr-worker.py:980/987` |
| 2.3 | P1 | `launch-reconcile` 无法回收泄漏 pane | `task_resources.py:249` |
| 2.4 | P1 | launch 失败必留 intent，无自助重试 | `bin/herdr-task` |
| 2.5 | P1 | preflight 快照过期**反向**放大故障 | `agent_router.py:544-549`（`:547` 为赋值行） |
| 2.6 | P1 | 快照永不自我刷新（探针不回写） | `deep_preflight.py` |
| 2.7 | P1 | recovery `idle` 分支 fail-closed，丢失的 DONE 永不补登 | `herdr-controller.py:9363-9368` |
| 2.8 | P1 | `node_usage` 累计计数含 `superseded` 墓碑 | `node_capacity.py:22-24`（`:24` 为 `selected` 赋值行） |
| 2.9 | P1 | `dispatch` 只接受 `pending`，`working` 无法回退 | `bin/herdr-task:3710` |
| 3.1 | P2 | 同节点无 Agent 多样性保证 | `agent_router.py:421-430` |
| 3.2 | P2 | `note-add --kind` 不支持 `review` | `bin/herdr-task:7733-7746` |
| 3.3 | P2 | `gitee-pr.sh create --head` 源恒为 HEAD | `scripts/gitee-pr.sh:269` |
| 3.4 | P2 | `node-status` 只读，无 CLI 改 `required_task_ids` | `bin/herdr-task:6356` |
| 3.5 | P2 | 项目 `workflow.json` 无 schema 校验，可手工注入非法值 | `projects.py` |
| 3.6 | P2 | ops-center 节点异常不给出原因 | `bin/herdr-task:794-808` |
| 3.7 | P2 | `current_stage` 字段残留为空 | `workflows.json` |

---

## 1. P0 — 阻断级

### 1.1 Agent 自建分支导致终化拒绝，且 `commits=0` 严重误导

**现象**

```
[REFUSED] reason=current_branch_mismatch head=8be3099a... commits=0 baseline=1638608cc...
[FINALIZE ESCALATED] reason=commit_refused
```

任务实际有 2 个合格提交，却报`commits=0`。

**根因**

worker 预先 `create_task_branch` 并 checkout 了 `task['branch']`，但 Agent 自行 `git switch` 到自建分支
（`agent/opencode/fix-compliance-request-race-json-truth`）。终化按任务分支取提交，自然取到 0 个。

归属校验本身是对的（`git_adoption.py:_check_current_branch`，P1 ownership），问题在**报错信息**：
`commits=0` 让人误以为"Agent 没干活"，实际是"在错误的分支上找"。

**证据**

```
clone 内任务分支 feat-impl-...  领先基线 0 个提交
clone 内自建分支 fix-...         领先基线 2 个提交
```

**建议**

1. 报错信息区分两种情形：分支不匹配 → 明示"当前分支 X，期望 Y"；分支匹配但无提交 → 才报 `commits=0`。
2. `write_task_context` 已把 `branch=` 写入 clone，可作为预期分支的事实来源，在终化时一并核对并给出明确差异。

**验证**：构造 Agent 切换分支的场景，断言错误信息包含 expected/actual 两个分支名。

---

### 1.2 Agent 提前 push + 自建 PR 导致终化拒绝（**协议死结**）

**现象**

```
[REFUSED] reason=foreign_commit_in_range head=8be3099a... commits=2 baseline=1638608cc...
```

**根因**

`git_adoption.py:260` 明确：

```python
def _check_remote_contained(commits, remote_shas):
    """H-2: any interval commit reachable from origin/* is foreign."""
```

`lessons-learned.md` F-1 亦记载「收尾节点自己的分支**不推送**」。Agent 推送并开了 PR !1513，
提交落入 `origin/*` 可达集 → 终化判为外来提交，拒绝认领。

**关键矛盾（这才是重点）**

| 约束 | 来源 | 要求 |
|---|---|---|
| 平台协议 | lessons F-1 | 收尾节点分支**不得推送** |
| 任务需求 | 用户需求 §六 | 「创建新的 PR 并保持待审查」 |
| 平台能力 | 实测 | Herdr **无法创建 PR** |

已核实：herdr-controller.py 无 PR 创建逻辑；`herdr-task` 无 PR 子命令；`check-delivery`仅做「建 PR 前校验」。
建 PR 只能靠仓库侧 `scripts/gitee-pr.sh`。

而 `supersede_task`（`bin/herdr-task:6530` 注释）自陈：

> 不补的后果不是「少个字段」：required_task_ids 的链式解析断在第一跳，实现节点永远判不出完成

即：**协议要求不推送，但需求要求 PR，而平台不产 PR**。三者不可同时成立。

**建议（需先定方向，见 §4）**

- 若坚持「Agent 建 PR」：终化需接受「本任务自己推送的分支」——可考虑记录推送时的 run_id/task_id 作为归属证据，而非仅凭 `origin/*` 可达性判断。
- 若坚持「终化前不推送」：任务需求模板应改为「由 Herdr integrate 阶段推送并建 PR」，且平台需补建 PR 能力。

**验证**：见 §4 决策后再补对应回归。

---

### 1.3 `required_task_ids` 跨 workflow → 节点永久无法完成

**现象**

任务 `status=integrated`、`stage_verdict=pass`，但节点 `status=pending`，且 **ops-center 不给任何原因**。

**根因**

项目定义 `~/.herdr-controller/projects/gemini-0b3b8aba/workflow.json` 中：

```json
"implementation": { "required_task_ids": ["impl-download-guard-compliance-truth"] }
```

该 task_id 属于 **`wf-project-1001-01`**（已 `cleaned`）。而两处解析都按 workflow 过滤：

- `scheduler.py:142` `by_id = {t.get("task_id"): t for t in tasks}`（`tasks` 为本工作流本节点）
- `workflow_continuation.py:14` `owned = [t for t in tasks if t.get("workflow_id") == wid]`

链式解析第一跳 `by_id.get()` 返回 `None` → `scheduler.py:149-150` `if task is None: return False`，**永久失败**。

**对照实验（已实测）**

| `required_task_ids` | `node_is_complete` |
|---|---|
| `None` | True |
| `['impl-download-guard-compliance-truth']`（原值） | **False** |
| `['impl-compliance-race-json-truth']`（修正值） | True |

**附带事实**

- 内置模板 `software-development-v1` 的 6 个节点 **`required_task_ids` 计数为 0** → 该字段非模板产物
- 同目录存在两个历史备份 `workflow.json.bak-before-impl-inventory`、`bak-before-wrapup-integration-fix`
  → 此文件**此前已被手工打过补丁**，属既有实践而非本次孤例

**⚠️ 不要用「让解析跨 workflow」来修**

`JOIN_MISSING_CANDIDATE = "join_missing_candidate"`（`scheduler.py:35`，返回点 `:555`、`:573`（`:35` 与 `:555`/`:573` 已复核））
是**刻意保留**的判决态，用于表达「要求的候选缺失」。若让解析静默跨工作流兜底，
「确实缺失」与「在别处」会被混为一谈，安全信号被抹掉。

**建议**

1. 解析失败时区分两态：required id 不存在于本工作流 → 新增判决（如 `join_required_task_out_of_workflow`），
   在 ops-center 节点卡片上**显示原因**，而非只显示 `pending`。
2. 项目 `workflow.json` 载入时校验 `required_task_ids` 的每个 id 是否属于本工作流，不合规则拒绝启动并报明确错误。

**本次处置**：仅改配置值指向本工作流任务（1 行，已备份 `workflow.json.bak-215101`），
**未改平台代码**，缺陷仍在。

---

### 1.4 `[AUTO ACCEPT]` 绕过验收门禁

**现象**

```
[REGISTRY WATCHER] status=agent_done -> supervisor-gated done event
[QUEUE] task=impl-compliance-race-json-truth event=done
[STATE] agent_done -> completed
[AUTO ACCEPT] node=implementation baseline=TASK_CHANGED -> completed
```

任务 `completed` 但 `stage_verdict=""` —— **门禁在无裁决的情况下放行**。本次 workflow 内发生两次
（requirements 节点的 `requirements-adversarial`、implementation 节点的本任务）。

**影响**

`verdict` 是「评审/测试任务查出缺陷必须 `blocked`」这类纪律的唯一落盘凭据。
空verdict 意味着节点推进时**没有任何验收记录**。

**建议**

- 若确需自动放行（无评审任务的节点），应在事件日志中标注 `auto_accept_reason`，
  并让 `stage_verdict` 落为显式的 `auto` 而非空串，避免下游把「空」与「已裁决」混同。
- 对含 `adversarial` / `review` 语义的节点，禁止 AUTO ACCEPT。

---

## 2. P1 — 流程可靠性

### 2.1 Supervisor 判 `work_off_track` 却仍放行

```
[SUPERVISOR] trigger=agent_done provider=jev action=PAUSE why=work_off_track 0.76 >= threshold;
             reroute disabled; hold the scene for inspection instead of burning attempts
[QUEUE]    event=done
[STATE]    agent_done -> completed
```

策略是「暂停现场以供检查」，实际却紧接着发`done` 并完成。**自相矛盾**。

本次人工复核认定该判定为**误报**（B 侧实现正确采纳了后端源码事实，A 侧显式处理了 A→B→A 陷阱），
推测误报由 §1.1 的分支偏离触发。但「判偏航却照常验收」的风险高于误报本身。

**建议**：`action=PAUSE` 时不得继续走 `done`；应产出独立判决态并阻塞节点。

---

### 2.2 launch 失败后 pane 泄漏

`herdr-worker.py:980` `create_pane(...)` 先于 `:987` `write_worker_launch_identity(...)`。
后者失败时回滚只清 clone（`[WORKER ROLLBACK]`），**不清理已建pane**，留下
`pane_present_needs_instance_reconciliation` 残态。

（`:987` 的 `FileNotFoundError` 本身已由 `26d9bb2` 修复，但**任何此处的失败**都会重现 pane 泄漏。）

**建议**：把 pane 纳入回滚资源集，或将`write_worker_launch_identity` 提前到 `create_pane` 之前。

---

### 2.3 `launch-reconcile` 无法回收泄漏 pane

```
$ herdr-task launch-reconcile ... --apply
{"resource_status": "unknown", "reason": "pane_present_needs_instance_reconciliation",
 "pane_id": "w13:p1T", "applied": false}
```

`task_resources.py:249` 对 `verdict != 'absent'` 直接返回 `recovery_required`，
而 `--apply` 对该状态无效 → **只能人工 `herdr pane close`**。

附带误导：探测时若带了不匹配的 `--dispatch-role`，会报 `launch intent not found`，
而实际 intent 存在（不带 role 才能找到）。错误信息应区分「intent 不存在」与「role 不匹配」。

**建议**：为 `pane_present_needs_instance_reconciliation` 实现真正的 instance 级对账，
使 `--apply` 能收敛；并修正 role 不匹配的错误文案。

---

### 2.4 launch 失败必留 intent，无自助重试

```
Task cannot dispatch from status: working     # exit 75
[DISPATCH IN_PROGRESS] ... reconcile intent-owned resources before retrying
```

每次失败都需人工 `launch-reconcile --apply` 才能重试。本次因重启中断产生 3 次。

安全设计，但缺少自助闭环，且与 §2.3 叠加后恢复路径较长。

---

### 2.5 preflight 快照过期会**反向**放大故障

`agent_router.py:544-549`（`:547` 为赋值行）：

```python
# 当快照新鲜时，排除所有已知不健康状态 (unhealthy_agents)；
# 当快照过期时，仅永久硬过滤致命状态 (hard_unhealthy)，
# 允许尝试 TIMEOUT / UNKNOWN 等非致命或瞬态状态。
excluded_unhealthy = unhealthy_agents if snapshot_fresh else hard_unhealthy
```

实测快照年龄 12102s（TTL=1800）时，`--agent auto` 选中了偏好序里的 `qodercli`，
其 clone 内 deep preflight 失败 → 派发崩溃。

**关键**：快照过期不等于「钉死在最后一个健康 Agent」，而是**放开选择到未验证 Agent**。
过期时的失效方向是危险的（fail-open）。

**建议**：过期时应 fail-closed（退回硬过滤 + 强制重跑探针），而非放宽。

---

### 2.6 快照永不自我刷新

`herdr-deep-preflight` 是独立二进制，执行后**不写回** `workflows.json` 的
`healthy_agents` / `unhealthy_agents` / `preflight_checked_at`。实测探针给出真实健康集
（`codex`/`qodercli`/`agy`/`grok` 真实通过，`claude` 真实 `code=1` 失败），但工作流记录纹丝不动。

结果：`preflight_checked_at` 永久停在派发时刻，随后必然过期，触发 §2.5。

**建议**：探针提供 `--apply` 回写，或派发前由 Controller 自动重跑并落盘。

---

### 2.7 recovery `idle` 分支 fail-closed，丢失的 DONE 永不补登

`herdr-controller.py:9363-9368`：

```python
# Agent 曾经进入 working/rework，随后 Controller 重启时发现已经 idle
if runtime == "idle":
    if current in {"working", "rework"}:
        has_done_marker, _screen = _completion_marker_snapshot(task)
        _record_completion_sample(task, has_done_marker, agent_status="idle")
    return
```

配合 `_completion_marker_snapshot`（`:9048`）依赖屏幕上的字面量 marker
（`completion.py:125-148`，`HERDR_TASK_DONE:<task_id>`）。

后果：Agent 进程若在写完产物后**异常退出**，TUI 消失 → marker 不存在 → `has_done_marker=False`
→ 任务永久卡在 `working`，且**所有补登通道均不可用**
（`report-completion` 未启用 receipt-v1、`launch-reconcile` 报 intent not found、无 identity 凭据）。

本次 requirements 节点的 `requirements-adversarial` 即因此卡死，最终只能 `supersede`。

**建议**：DONE 证据不应只依赖可被销毁的屏幕文本；应在 Agent 侧落一个持久完成标记文件，
或让 Controller 在采样时结合 clone 内产物指纹做兜底判定。

---

### 2.8 `node_usage` 累计计数含 `superseded` 墓碑

`node_capacity.py:22-24`（`:24` 为 `selected` 赋值行）：

```python
selected = [t for t in tasks if (t.get("node") or t.get("stage")) == nid
            and (workflow_id is None or t.get("workflow_id") == workflow_id)]
```

`selected` **不按 status 过滤**，`task_count = len(selected)` 把 `superseded` 墓碑计入。
后果：节点累计达上限后，即便旧任务已作废，新建替代者仍触发 legacy `max_agents` 门禁，
只能 `--ack-overflow`（审计豁免）突破。

**建议**：`total_limit` 判定排除终态 `superseded`，或区分「累计注册数」与「活跃数」两个口径。

---

### 2.9 `dispatch` 只接受 `pending`

`bin/herdr-task:3710-3712`：

```python
if task["status"] != "pending" and not (receipt_delivery and task["status"] == "dispatched"):
    print(f"Task cannot dispatch from status: {task['status']}")
```

`transitions.py` 中 `working` 的合法出边为
`{blocked, agent_done, rework, paused, failed, superseded, interrupted}` —— **无回到 `pending` 的边**。

后果：卡在 `working` 的任务无法重跑，只能 `supersede` + `launch` 新任务（换 task_id、换 clone、占新 pane）。

---

## 3. P2 — 可用性与可观测性

### 3.1 同节点无 Agent 多样性保证

`agent_router.py:421-430` 的 `stage_used_agents` 仅做**跨阶段**隔离
（`exclude_stages`，如 `test`/`review` vs `implementation`），**同节点内无去重**。
`parallel: true` 的节点两个 Task 可落在同一 Agent。

本次 plan 节点首派即命中（两 Task 同为 opencode），导致对抗审查者与主架构师同模型。

### 3.2 `note-add --kind` 不支持 `review`

`bin/herdr-task:7733-7746` 可选值：
`requirement / spec / plan / decision / evidence / gate / delivery / invalidation / note / wrapup` —— **无 `review`**。

Agent 按 prompt 调用 `note-add --kind review` 得到 exit 2，只能降级用 `kind=plan` 存储 review 摘要，
共享区条目语义与 `kind` 不符。建议补 `review`（或允许 `kind` 自定义 + `category` 分离）。

### 3.3 `gitee-pr.sh create --head` 源恒为 HEAD

`scripts/gitee-pr.sh:269`：

```bash
if ! git push origin "HEAD:$head_branch"; then
```

`--head` 只设置**目标 ref**，推送源恒为当前 HEAD。在锚定分支执行时会把**锚定分支内容**推向该 ref。

本次因 non-fast-forward 被拒而**未造成损失**，但若锚定分支恰好可快进覆盖，
会**静默覆盖任务分支**。建议：`--head` 指定时校验 `HEAD == head_branch`，否则直接拒绝。

### 3.4 `node-status` 只读，无 CLI 修改 `required_task_ids`

`bin/herdr-task:6356` 的 `node_status` 仅查询。修正 §1.3 只能手工编辑
`projects/<id>/workflow.json`，无校验、无留痕。

### 3.5 项目 `workflow.json` 无 schema 校验

`required_task_ids` 可被手工注入指向不存在的 task_id，且启动时不校验（§1.3 即由此产生）。
`workflow.py:364-365` 的 normalizer 只做透传复制，不校验取值。

### 3.6 ops-center 节点异常不给原因

`bin/herdr-task:794-808` 的节点 status 只有 `empty/superseded/failed/blocked/completed/in_progress/pending`，
`pending` 不携带任何 `reason`。§1.3 排查耗时主要源于此。

### 3.7 `current_stage` 字段残留为空

`workflows.json` 中 `wf-project-1002-01` 的 `current_stage = ""`，而工作流推进正常。
该字段疑似死字段或未被维护，建议确认后清理或补齐。

---

## 4. 需要你决策的协议矛盾

### 4.1 谁负责创建 PR

| 方案 | 优点 | 代价 |
|---|---|---|
| **A. 平台支持 Agent 建 PR** | 满足现有任务需求模板 | 终化的 `foreign_commit_in_range` 需为「本任务自推」开归属凭据，削弱 H-2 保护 |
| **B. Herdr integrate 建 PR** | 守住 F-1，归属清晰 | 平台需补建 PR 能力；任务需求模板需改写 |

在 A/B 之间定调前，§1.2 无解。

### 4.2 `required_task_ids` 指向跨 workflow task_id 时的语义

- 视为**配置错误**（应在项目定义校验期拒绝）→ 采纳 §1.3 建议 2
- 视为**合法的跨工作流续接门禁** → 需扩展解析，但必须新增独立判决态以保留 `JOIN_MISSING_CANDIDATE` 的安全信号

---

## 5. 已修复（供参考，本次未参与）

| 项 | 内容 |
|---|---|
| 提交 | `26d9bb2` fix(worker): preserve launch identity across sandbox sanitize during task dispatch |
| 合并 | `c0cf2a0`（Merge PR #141） |
| 方案 | `git clean -fd -e .herdr-launch-identity.json` |
| 测试 | `tests/test_worker_sanitize_sandbox.py` |
| 详细交接 | `docs/context/herdr-worker-launch-identity-handoff.md` |

---

## 6. 附录 A：本次 workflow 的交付状态（供交叉核对）

| 项 | 值 |
|---|---|
| 任务 | `impl-compliance-race-json-truth`，`integrated` + `stage_verdict=pass` |
| 提交 | `6f5fb1c18`（取数竞态）、`8be3099a6`（JSON 导出真实性） |
| 改动 | 4 文件 +877/-22，均在允许范围内 |
| 测试 | 42 + 9 = 51 条 |
| PR | #1514 → https://gitee.com/allinai888/dianzikuaijidangan/pulls/1514（待审查，未合并） |

---

## 7. 附录 C：本次 workflow 侧（非 HAFlow）问题

以下属流程纪律问题，不在 HAFlow 修复范围，但影响本次交付质量：

1. **requirements 节点的 `pass` 是不完整的。** 对抗审查提出 BL-1/BL-2/BL-3 三个 Blocker，
   Controller 因`requirements-adversarial` 已被 `superseded`（退出门禁计算）而按 `requirements-spec` 的
   `pass` 放行，**三个 Blocker 从未裁决**。已在 implementation 阶段强制补裁。
2. **plan 与其对抗审查存在事实矛盾未在节点内裁决。**
   `complianceLevel`「6 形态」（plan）与「后端仅 3 中文输出、6 为前端兼容集」（review）表述冲突，
   照抄 plan 字面会写出永不触发的死代码。已在 implementation 阶段由实现方核实后端源码消解。
3. **实现偏离一处需求字面要求。** 需求要求「在下载副作用边界观察 `createObjectURL`/`anchor.click` 为 0」，
   实现改用更早的 `downloadBlob` 零调用边界（语义更强但形式不同）。已记入验收 note，建议后续补断言。
4. **派发 prompt 缺陷（我的责任）。** prompt 中「分支名须与实际 agent 一致，用 `agent/<你的agent>/...`」
   反而暗示 Agent 可自建分支，直接导致 §1.1。正确表述应为「必须留在 Herdr 分配的分支上，不得切换或新建」。

---

## 8. 复核命令

```bash
cd ~/HAFlow

# §1.3 因果对照
python3 - <<'PY'
import json,sys; sys.path.insert(0,'~/HAFlow')
from herdr.scheduler import node_is_complete
ts=json.load(open('/Users/user/.herdr-controller/tasks.json')); ts=ts.get('tasks',ts)
impl=[t for t in ts if t.get('workflow_id')=='wf-project-1002-01'
      and (t.get('node') or t.get('stage'))=='implementation']
w=json.load(open('/Users/user/.herdr-controller/projects/gemini-0b3b8aba/workflow.json'))
cfg=[n for n in w['nodes'] if n['id']=='implementation'][0]
print(cfg.get('required_task_ids'), node_is_complete(impl,cfg.get('required_task_ids')))
PY

# §2.5 preflight 过期分支
sed -n '544,549p' herdr/agent_router.py

# §2.8 累计计数
sed -n '22,24p' herdr/node_capacity.py

# §3.2 note-add kind 取值
sed -n '7733,7746p' bin/herdr-task

# §3.3 gitee-pr 推送源
sed -n '269p' /Users/user/nexusarchive-worktrees/gemini/scripts/gitee-pr.sh
```