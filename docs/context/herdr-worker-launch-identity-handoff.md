# Handoff：`sanitize_clone_sandbox` 删除 launch identity，导致所有新 Task 派发失败

> 状态：**待修复**。当前线上靠一个用户级 git ignore 绕过（见 §6），该绕过不通用。
> 环境：`~/HAFlow`，release `22fb1a9d2463a766e884ca6d429f01915660050f`，分支基线 `6d5ba25`
> 复现仓库：`/Users/user/nexusarchive-worktrees/gemini`（worktree，`.git` 为文件型指针）
> 报告时间：2026-10-02

---

## 1. 症状

`herdr-task launch` 对**任何**新 task_id 都在 worker 阶段崩溃，Task 无法注册：

```
Worker failed: HERDR_WORKER_FAILURE={..."failure_type":"FileNotFoundError","agent_started":false,...}
Traceback (most recent call last):
  File ".../services/herdr-worker.py", line 955, in main
    write_worker_launch_identity(clone, launch_identity)
  File ".../herdr/task_resources.py", line 262, in write_worker_launch_identity
    previous = json.loads(path.read_text())
FileNotFoundError: [Errno 2] No such file or directory:
  '<clone>/.herdr-launch-identity.json'
```

伴随现象：`[WORKER ROLLBACK] Cleaning up incomplete clone`，clone 被回滚，但**pane 泄漏**
（`create_pane` 已在 `:948` 执行，`:955` 才失败），留下无法回收的虚拟 pane。

影响范围：**通用缺陷，非特定仓库**。任何用 `git clean -fd` 且未忽略该文件的仓库都会命中。

---

## 2. 根因

`services/herdr-worker.py:107-114`：

```python
def sanitize_clone_sandbox(clone):
    """Purge uncommitted working tree edits and untracked files copied into the clone sandbox."""
    subprocess.run(["git", "-C", str(clone), "reset", "--hard", "HEAD"], capture_output=True)
    subprocess.run(["git", "-C", str(clone), "clean", "-fd"], capture_output=True)   # ← 元凶
```

`main()` 的执行顺序：

| 行号 | 动作 | `.herdr-launch-identity.json` 状态 |
|------|------|--------------------------------|
| `:849-850` | 构造 `launch_identity` | — |
| `:874-876` | `write_worker_launch_identity(..., initial=True)` | **创建**（未跟踪文件） |
| `:885-891` | `create_task_branch(...)` → `:243` `sanitize_clone_sandbox` | **`git clean -fd` 删除** |
| `:937` | `verify_request_preflight` | 已消失 |
| `:948` | `create_pane(...)` | pane 已创建（此后失败 → 泄漏） |
| `:953-955` | `write_worker_launch_identity(...)`（`initial=False`） | **回读 → FileNotFoundError** |

`task_resources.py:256-264` 中 `initial=False` 分支**故意**回读既有文件做归属校验：

```python
if not initial:
    previous = json.loads(path.read_text())   # :262  未捕获 FileNotFoundError
```

这个校验本身是对的（防止 worker 认领他人资源），**不应改成容错**。错的是清理步骤跑在打标之后。

### 三个调用点全部中招

`sanitize_clone_sandbox` 有 3 处调用，**全部**位于 `:876` 之后：

| 行号 | 所属函数 | 触发条件 |
|------|----------|----------|
| `:243` | `create_task_branch`（`:203`） | 无 `--onto` 的普通路径 ← **本次故障路径** |
| `:272` | `checkout_onto_branch`（`:263`） | `pinned_local_onto_matches` 成立 |
| `:308` | `checkout_onto_branch` | `--onto` 主路径 |

→ **修法必须落在 `sanitize_clone_sandbox` 函数内部**，改调用点会漏。

### 为什么仓库的 `.gitignore` 挡不住

`/Users/user/nexusarchive-worktrees/gemini/.gitignore:337` 只有 `.herdr/`（**目录**），
而 worker 写的是**文件** `.herdr-launch-identity.json`（仓库根目录，两者在 glob 上不匹配）：

```
$ git check-ignore -v .herdr-launch-identity.json   # → 无输出，未被忽略
$ git clean -nd
Would remove .agent-task-context
Would remove junk.txt            # ← identity 也会被删，只是 -n 未实际执行
```

注意 `git clean -fd` **会**删未跟踪的点文件（`.agent-task-context` 就在删除列表里），
它只跳过**被忽略**的文件；而忽略需要 `-x` 才会被 `clean` 遵从删除。

---

## 3. 复现步骤

```bash
T=$(mktemp -d); git init -q "$T"; cd "$T"
echo x > a.txt; git add -A; git -c user.email=a@b -c user.name=c commit -qm init
git config core.excludesFile /dev/null          # 必须！屏蔽全局 ignore，否则测不出
echo '{}' > .herdr-launch-identity.json; echo j > junk.txt
git clean -fd >/dev/null
ls .herdr-launch-identity.json 2>&1             # → No such file（BUG 复现）
```

> **坑**：仅设 `GIT_CONFIG_GLOBAL=/dev/null` **无效**。git 无条件读取
> `$XDG_CONFIG_HOME/git/ignore` 作为全局 excludes，与 `core.excludesFile` / `~/.gitconfig` 无关。
> 只有 `git config core.excludesFile /dev/null` 能真正隔离。

端到端复现（会真实消耗 provider 调用，谨慎）：

```bash
~/HAFlow/bin/herdr-task launch --task-id diag-probe --workflow-id <wf> \
  --node <node> --source <repo> --agent opencode --task-type docs \
  --integration-mode none --dispatch-role <role> \
  --goal "..." --acceptance "..." --prompt "..."
# → FileNotFoundError on .herdr-launch-identity.json
```

---

## 4. 推荐修法（一行，落在函数内部）

```diff
--- a/services/herdr-worker.py
+++ b/services/herdr-worker.py
@@ -111,7 +111,10 @@ def sanitize_clone_sandbox(clone):
     Since the clone sandbox is an isolated CoW copy, resetting it does not touch the source repo,
     ensuring that subsequent branch switches never collide with developer WIP in the source repo.
     """
     subprocess.run(["git", "-C", str(clone), "reset", "--hard", "HEAD"], capture_output=True)
-    subprocess.run(["git", "-C", str(clone), "clean", "-fd"], capture_output=True)
+    subprocess.run(
+        ["git", "-C", str(clone), "clean", "-fd",
+         "-e", ".herdr-launch-identity.json"],
+        capture_output=True,
+    )
```

理由：
- **一处改动覆盖 `:243` / `:272` / `:308` 三个调用点**
- **不依赖任何仓库的 `.gitignore`**（`-e` 是显式 exclude，与 ignore 规则无关）
- **保留"破坏性操作前先打归属标记"的安全语义**（见 §5 为什么不选调顺序）
- 无行为副作用：`-e` 只多保护这一个文件名，其余未跟踪文件照常清理

## 5. 已评估并否决的替代方案

**否决 A：把身份标记写入移到 `sanitize_clone_sandbox` 之后（调顺序）**

看似更贴合"清理 sandbox 应在打标前"的原意，但**破坏崩溃恢复语义**：
标记的用途是在破坏性操作**之前**落盘证明资源归属。若 clean 期间崩溃，
clone 内将没有任何归属标记，`probe_resources` 只能返回 `unknown`
（而非 `absent`），进而走进 `pane_present_needs_instance_reconciliation` 死路——
`herdr-task launch-reconcile --apply` 对该状态**无效**（实测 `applied: false`），
只能人工 `herdr pane close`。这正是本次反复遇到的状态。**不要走这条路。**

**否决 B：让 `task_resources.py:262` 容忍文件缺失**

`initial=False` 的回读是**故意**的归属校验（防止 worker 认领他人 clone）。
吞掉 `FileNotFoundError` 等于取消安全检查。**不要改这里。**

**否决 C：在 NexusArchive 的 `.gitignore` 里加豁免**

只治一个仓库，且把平台 workaround 污染进业务仓库的提交面。**不要走这条路。**

---

## 6. 验证结果（已实测，非推演）

干净环境（`core.excludesFile=/dev/null`）：

| 场景 | identity | 无关未跟踪文件 |
|------|----------|----------------|
| 现状 `clean -fd` | **被删** ← BUG 复现 | 已清 |
| 修法 `clean -fd -e .herdr-launch-identity.json` | **存活** ✓ | 已清 ✓ |
| 修法下对已忽略文件 | 保留（`-fd` 不带 `-x`，语义未变）✓ | — |

### 当前生效的临时绕过（**非修复**，通用性差）

```bash
# 已写入 /Users/user/.config/git/ignore 第 5-9 行
.herdr-launch-identity.json
```

作用：让 `git clean -fd` 跳过该文件。**只对这台机器的 git 环境有效**，
其他机器 / 其他仓库 / CI 环境仍会踩。

回退方式：删除 `~/.config/git/ignore` 中该行及其上方 4 行注释。

> ⚠️ 若采纳 §4 修法并验证通过，**请移除该全局 ignore**，避免长期掩盖同类缺陷。

---

## 7. 验证清单（修复后）

```bash
# 1. 目标测试
cd ~/HAFlow && python3 -m pytest tests/ -k "clone or launch or worker" -q

# 2. 端到端：新 task_id 能注册
~/HAFlow/bin/herdr-task launch --task-id verify-fix-01 --workflow-id <wf> \
  --node <node> --source <repo> --agent opencode --task-type docs \
  --integration-mode none --dispatch-role <role> \
  --goal "..." --acceptance "..." --prompt "..."
# 期望：[REGISTERED] verify-fix-fix-01，且**无残留 pane**

# 3. --onto 路径同样验证（覆盖 :272 / :308）
#    加 --onto <existing-branch> --candidate-sha <sha> 重跑

# 4. 确认 clone 内标记最终落地
cat ~/.herdr-controller/clones/<task>/.herdr-launch-identity.json
```

补充断言建议：为 `sanitize_clone_sandbox` 加单测，断言
`git clean -fd` 后 `.herdr-launch-identity.json` 仍存在、且另一个未跟踪文件被删。
当前 `tests/` 无此覆盖，是缺陷得以存活的直接原因。

---

## 8. 顺带发现的其它缺口（同批次，建议一并登记）

均**未修复**，仅记录：

1. **`launch-reconcile` 无法处理 `pane_present_needs_instance_reconciliation`**
   `herdr-task launch-reconcile --apply` 对该状态返回 `applied: false`，需人工
   `herdr pane close <pane>`。worker 在 `:955` 类失败后必然留下这类残态，建议补齐。

2. **worker 失败后 pane 泄漏**
   `create_pane`（`:948`）先于 `write_worker_launch_identity`（`:955`），
   后者失败即无回收。回滚只清了 clone（`[WORKER ROLLBACK]`），没清 pane。

3. **每次失败都留 launch intent，需人工 reconcile 才能重试**
   表现为 `Task cannot dispatch from status: working`（exit 75，
   `[DISPATCH IN_PROGRESS] ... reconcile intent-owned resources before retrying`）。
   安全设计，但缺少自助闭环。

4. **同节点无 Agent 多样性保证**
   `stage_used_agents` / `exclude_stages` 只做**跨阶段**隔离；同一 node 内两个 Task
   可落在同一 Agent。Router 无同节点去重。

5. **preflight 快照过期会反向放大故障**
   `agent_router.py:544-549`：快照新鲜时排除全部 `unhealthy_agents`；过期时仅硬过滤
   `hard_unhealthy`，从而**放开** TIMEOUT/UNKNOWN 的 Agent 按偏好序被选中——
   而这些 Agent 未必真健康。实测一次失败派发即由此产生（选中 qodercli，探针 ERROR）。
   合理设计，但"快照过期"应偏向保守而非放开。

---

## 9. 提交纪律提醒

- **不要**在 `fix/preflight-hardening-router-optimization` 分支上直接改（该分支已有他人未合并工作，基线 `6d5ba25`，工作区当前干净）
- 建议另开分支，如 `fix/worker-clone-cleanup-preserve-launch-identity`
- 本仓库远端与 release 机制需注意：Controller 实际运行的是
  `~/.herdr-controller/releases/<sha>/bin/herdr-task` 快照，
  **改 `~/HAFlow` 不影响正在运行的 Controller**——修复需走发布流程才会对 Controller 生效