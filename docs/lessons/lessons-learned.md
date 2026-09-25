# 通用工程教训与复盘 (Lessons Learned)

> 本文件记录跨模块、可复用的核心工程教训与 SOP，按 session 追加章节。
>
> **编写纪律**：
> 1. 只记录具备通用指导意义的教训，不记录一次性单点业务逻辑；
> 2. 必须遵循四段式结构（问题背景 → 经验教训表格 → 操作规范 → 验证命令/证据）；
> 3. 每条教训必须有真实日志、PR 或代码证据，禁止虚构推测；
> 4. 同一类问题复发 2 次以上，必须推动升格为自动化门禁（pre-push 脚本 / pytest 架构测试 / AGENTS.md 底线）；
> 5. 已被新架构或全量门禁覆盖的历史单点内容，定期归档至 [`lessons-archive.md`](./lessons-archive.md)。

---

## 生命周期闭环

```
真实事故 / 复杂排查
       │
       ▼
1. 单点 RCA 根因分析 (root-cause-analysis-*.md)
       │ 提炼出跨模块、通用性的工程教训
       ▼
2. 沉淀入册 (本文件追加章节)
       │ 同类教训复发 2 次以上
       ▼
3. 规则固化 (pre-push 门禁脚本 / pytest 架构测试 / AGENTS.md 底线)
       │ 已有强门禁覆盖
       ▼
4. 归档瘦身 (单点过时记录移入 lessons-archive.md)
```

---

## 1. LaunchAgent 进程热重载陷阱

### 问题背景

多次出现修改 `services/herdr-controller.py` 后，后台调度行为仍是旧逻辑，排查耗时严重。
根因是 LaunchAgent 进程常驻内存，不会自动热加载 Python 源码。
关联坑点：`RULES.md §4 坑点1`，Git Commit `16025ac`（fix(task): propagate next_stage）。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 修改源码后直接在终端重启进程 | LaunchAgent 与直接 python3 启动会发生 Socket / 状态文件冲突 | 必须用 `launchctl kickstart -k` 热重载，严禁裸启 |
| 修改后未验证进程 PID 是否更新 | 代码更新成功不等于运行中进程已切换 | 热重载后必须用 `launchctl list | grep herdr` 确认 PID 变化 |
| 日志仍是旧逻辑输出 | 日志时间戳是判定"进程是否已切换"的铁证 | 重载后检查 `~/Library/Logs/herdr/*.log` 中的启动时间戳 |

### 操作规范（已固化到 `RULES.md §4 坑点1`、`CLAUDE.md`）

1. **修改 `services/` 任意文件后**：必须执行热重载命令，不得跳过。
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.user.herdr-controller
   ```
2. **验证进程已切换**：对比热重载前后的 PID。
3. **禁止操作**：`python3 services/herdr-controller.py` 直接前台运行（Socket 冲突）。

### 验证命令 / 守护测试

```bash
# 验证热重载后进程 PID 已更新
launchctl list | grep herdr
# 期望：PID 列非空且与重载前不同，ExitStatus 为 0

# 验证无孤儿进程
pgrep -a python3 | grep herdr
# 期望：只有一个 herdr-controller 进程
```

### 相关文档 / 关联证据

- `RULES.md §4 坑点1` — 已固化的操作红线
- `CLAUDE.md §服务管理` — 快速命令参考
- `docs/operations/service-management.md` — 完整 LaunchAgent 运维手册

---

## 2. CoW Clone 验收假象：裸 git status 不可信

### 问题背景

在 CoW Clone 任务目录中使用 `git status`，显示大量"未提交改动"，
误判为 Agent 当前 session 的产出，导致合并时混入主干的未提交变更。
关联坑点：`RULES.md §4 坑点2`。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| CoW Clone 创建时继承了主干未提交文件 | `git status` 无法区分"任务前存量"与"本次产出" | 必须用 `herdr-task verify-baseline` 以快照为准 |
| Agent 直接 `git add .` 提交了不相关文件 | 验收必须锁定真实产出边界，不能靠人工肉眼区分 | PR 合并前强制执行 baseline 验证 |

### 操作规范（已固化到 `RULES.md §4 坑点2`）

1. **严禁裸 `git status`**：CoW Clone 目录中禁止以此判定任务产出。
2. **必须使用**：
   ```bash
   ./bin/herdr-task verify-baseline <task-id>
   ```
   只有 `TASK_CHANGED` 下列出的文件，才是当前任务的真实产出。
3. **Agent 提交前**：必须 baseline 验证通过，再执行 `git add`。

### 验证命令 / 守护测试

```bash
./bin/herdr-task verify-baseline <task-id>
# 期望：输出 TASK_CHANGED 文件列表，无 UNEXPECTED_DIFF 警告
```

### 相关文档 / 关联证据

- `RULES.md §4 坑点2` — 已固化红线
- `bin/herdr-task` — verify-baseline 实现

---

## 3. Stage-Advance 竞态：并发节点同时推进导致状态不一致

### 问题背景

DAG 多节点并发完成时，`herdr-controller.py` 中的 stage-advance 逻辑发生竞态：
两个 worker 同时触发 stage 推进，导致部分节点被重复派发或跳过。
关联修复：Git Commit `16025ac`（fix: propagate next_stage）、
Commit `53ff474`（feat: DAG controller refactor）。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| stage-advance 未做幂等保护 | 并发调度下无锁的状态写操作必然产生竞态 | stage 推进必须加文件锁 + 幂等检查（已推进则跳过）|
| 状态文件直接 JSON 覆盖写 | 覆盖写在并发下是非原子操作 | 状态更新必须走原子 rename（write-tmp → rename）|
| 缺少并发回归测试 | 竞态 bug 在单线程测试中不可见 | 必须有 `--parallel` 参数的并发回归测试覆盖 |

### 操作规范（已固化到 `services/herdr-controller.py`）

1. **所有 stage-advance 路径**：必须持有 `workflow_state.lock` 文件锁再读写状态。
2. **状态文件写入**：使用 `write-tmp → os.replace` 原子写，禁止直接覆盖。
3. **幂等保护**：写前检查当前 stage，已推进则直接返回，不重复执行。

### 验证命令 / 守护测试

```bash
# 运行 stage-advance 与 supersede 回归测试
pytest tests/test_stage_advance_and_supersede.py -v
# 期望：所有用例 PASSED

# 并发压力验证（如有 parallel fixture）
pytest tests/ -v --tb=short
```

### 相关文档 / 关联证据

- `tests/test_stage_advance_and_supersede.py` — 回归测试（Commit `53ff474`）
- `services/herdr-controller.py` — 调度核心实现
- `wiki/dag-workflow-engine.md` — DAG 调度算法文档

---

## 4. 任务派发通知静默：Agent 无法感知任务到达

### 问题背景

早期 `herdr-task` 在派发任务后没有 macOS 通知，Agent 在另一个 Tab 工作时
无法感知新任务到达，导致任务在 READY 状态停留过久。
关联实现：Commit `53ff474` 新增 `herdr-notifier.py` 集成。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 派发动作无任何外部信号 | 多 Agent 并发场景下"任务等待"是隐性阻断源 | 关键状态变更（派发/完成/失败）必须有通知钩子 |
| 依赖 Agent 主动轮询感知 | 轮询带来延迟且消耗上下文 | 改为 push 式：派发即通知，完成即广播 |

### 操作规范（已固化到 `bin/herdr-task`、`services/herdr-notifier.py`）

1. **任务派发后**：`herdr-task` 自动调用 `herdr-notifier.py` 发送 macOS 通知。
2. **通知内容**：包含 task-id、node label、目标 Agent 类型。
3. **新增状态变更点**：必须评估是否需要补充通知钩子。

### 验证命令 / 守护测试

```bash
# 派发一个测试任务，验证通知是否弹出
./bin/herdr-task dispatch <workflow-id> <node-label> --dry-run
# 期望：终端输出 "Notification sent" 且 macOS 弹出通知横幅
```

### 相关文档 / 关联证据

- `services/herdr-notifier.py` — 通知服务实现
- `bin/herdr-task` — 派发集成点
- `wiki/task-lifecycle.md` — 任务生命周期状态机

---

## 5. Workflow 启动竞态：阶段事件先到、需求正文丢失

### 问题背景

Factory Console 启动 Workflow 时，`herdr-factory` 先写入 Workflow Registry；Controller
周期扫描到新 Workflow 后立即发送 `start → requirements`，而需求正文仍只存在于
`herdr-factory` 的进程参数中，尚未进入 Registry 或总指挥 Pane。Deep Preflight 完成后，
原实现再尝试直接向同一个总指挥 Pane 注入需求，造成 Controller 与 Factory 双写同一 Pane
的竞态。实际证据是 Workflow `wf-nexusarchive-54433229-20260912-194638` 已进入
`requirements`、Task 数为 0，总指挥 Pane 处于 working 但不知道具体需求。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 需求正文只存在于启动进程参数 | Workflow ID、阶段状态和用户需求必须属于同一个持久化启动记录 | Registry 必须保存 `requirement` |
| 注册即触发 Controller 推进 | “已注册”不等于“可派发” | 用 `startup_ready` 门闩隔离注册、预检和首次派发 |
| Factory 与 Controller 同时写总指挥 Pane | 同一会话不能有两个未协调的消息生产者 | Controller 作为首次节点消息的唯一投递者 |
| 队列事件可能早于状态修复进入内存队列 | 只在入队处检查状态不够 | 队列消费者也必须重新检查启动门闩 |
| Dashboard 保留旧 Workflow ID | 当前选中对象和最新 Job 对象可能分离 | 启动成功后必须用 Job 返回的 Workflow ID 更新前端上下文 |

### 操作规范

1. 启动时先写入 `requirement` 和 `startup_ready=false`。
2. Deep Preflight、固定 Agent 校验和策略写入全部完成后，才设置 `startup_ready=true`。
3. Controller 只消费 `startup_ready=true` 的启动记录，并从 Registry 组装总指挥消息。
4. 队列消费者再次检查启动门闩；旧队列事件不能绕过启动协议。
5. 发生中断恢复时，优先检查 Workflow Registry、`stage-state.json`、Task Registry 和
   总指挥 Pane 四层状态，不要只看单个 HTTP 返回码。

### 验证命令 / 证据

```bash
python3 -m unittest tests/test_workflow_start_sync.py
python3 -m unittest discover -s tests -p 'test_*.py'
./bin/herdr-task stage-status wf-nexusarchive-54433229-20260912-194638 requirements
herdr agent get wA:p1
herdr pane read wA:p1 --source visible
```

实际修复证据：Controller 日志出现 `STARTUP WAIT`，预检完成后 Registry 的
`startup_ready` 变为 `true`；清理当前 Workflow 的 requirements 锁并重新评估后，
`wA:p1` 标题变为“零号病人 Bug 责任链追溯脚本需求分析”。回归测试覆盖 Registry
需求持久化、启动门闩、Factory 不再直接投递和队列消费者二次检查。

---

## 6. Agent 负载与预检状态的误算与口径不一致

### 问题背景

控制台看板与 Agent 路由在计算 Agent 负载时，此前将 `ACTIVE` 状态集合设定为了包含 `completed`、`committed`、`integrated`、`cleanup_ready` 等已终结/后置状态。当某一 Agent（如系统未安装的 `codex`）在历史 Workflow 中曾被分配过任务且任务已完成时，在看板上仍会被误算为 `负载 7`。同时，控制台浅层预检在判断二进制和认证路径时使用了过简逻辑，导致控制台看板状态与真实探针存在偏差。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 把已完成的任务统计为 Agent 负载 | Task 终结状态（completed, integrated等）不代表 Agent 仍处于占用状态 | Agent 负载计算只计入处于在途状态（pending, dispatched, working, blocked, agent_done, rework）的 Task |
| 未安装的 Agent 却显示历史负载 | 已终结任务不应膨胀未安装 Agent 的在线负载 | 负载定义统一收窄为运行中/在途状态 |
| 控制台浅层预检二进制名与认证路径缺失 | 模块间二进制名（如 qodercli 对应 qodercn）和认证路径必须保持一致 | 控制台与 preflight 探测字典保持单点事实来源 |

### 操作规范

1. `_active_agent_loads()` 及 `agent_loads()` 必须统一只包含在途任务状态 `IN_FLIGHT_STATUSES`。
2. 控制台与 `preflight.py` 共享 `AGENT_BINARIES` 与 `AUTH_HINTS` 字典判定。
3. 修改控制台源码 `console/herdr_factory_console.py` 后必须运行 `scripts/install-herdr-console.sh` 热同步到 `~/.herdr-console`。

### 验证命令 / 证据

```bash
pytest tests/test_agent_router_loads.py
bash scripts/install-herdr-console.sh
```

---

## 7. Superseded 任务统计口径漂移：同一语义多处手写必然漏改

### 问题背景

`herdr-task launch --supersedes` 将 plan-t2 取代为 plan-t2-rev 后，Workflow 实际早已全部完成
（`stage-status` 判 completed、controller 判 complete、DAG 正常推进到 13/13），但运维驾驶舱的
节点卡片统计把 superseded 计入分母却不计入 completed，节点落到 `pending`；Workflow 详情页的
`stage_summary` 则因 superseded 不在任何状态集合里而落到 `mixed`，UI 渲染为"处理中"。
同一语义（排除被取代任务）在仓库里有 4 处独立手写实现：`is_node_complete`
（services/herdr-controller.py）、`node_status`（bin/herdr-task）、`_node_task_status_counts`
（bin/herdr-task）、`stage_summary`（console），supersede 特性落地时只同步了前两处。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 节点卡片把 superseded 计入分母 | 展示聚合层必须与调度判定层使用同一谓词，否则 UI 与事实分裂 | 统一谓词：`status == "superseded" or superseded_by 存在` |
| console stage_summary 落到 mixed | 状态枚举扩容（新增 superseded）时，所有 if/else 链都要重新审视 else 分支 | 新增终态后，聚合函数必须显式处理或过滤，不允许落入兜底分支 |
| 口径类缺陷第二次复发 | 这是 lessons 第 6 条（Agent 负载口径）之后的同类问题 | 已按纪律升格为 pytest 门禁：`TestOpsCardParity` 钉死卡片与 `is_node_complete` 的一致性 |

### 操作规范

1. 涉及"被取代任务"的任何统计，统一复用组合谓词 `status == "superseded" or task.get("superseded_by")`，
   与 `is_node_complete` / `node_status` 逐字一致；修改任一处必须同步其余处。
2. `_node_task_status_counts` 输出独立的 `superseded` 计数桶，节点/工作流级 `total` 只含存活任务；
   全退役节点用 `superseded` 状态展示，不得伪装成 `empty` 或 `pending`。
3. console `stage_summary` 的 `count` 为存活任务数，`tasks` 列表保留全部记录以维持退役任务可见性。
4. 为口径一致性新增 pytest 门禁后，任何新增统计消费方（新视图/新脚本）都应补对应 parity 用例。

### 验证命令 / 证据

```bash
pytest tests/test_herdr_task_ops_center.py tests/test_stage_advance_and_supersede.py tests/test_console_stage_summary.py
bash scripts/install-herdr-console.sh
curl -s "http://127.0.0.1:8765/api/ops-center?workflow_id=wf-nexusarchive-54433229-20260912-194638"
```

实际修复证据：`/api/ops-center` 返回 plan 节点 `total:2 completed:2 superseded:1 status:completed`，
`/api/workflow` 全部 stage 为 `cleaned`（修复前为 `mixed`→"处理中"）；
drilldown 从最老的 plan-t1 变为权威的 plan-t2-rev。
数据修补记录：plan-t2 已归一为 cleaned（备份 `~/.herdr-controller/backups/tasks.json.bak-20260912-230513`）。

## 8. 已装 Agent 被误判"未安装"：`shutil.which` 依赖服务进程 PATH，且映射三处手写

### 问题背景

共事工厂控制台"执行者阵容"把 codex/claude/qodercli/agy 显示为"未安装"，但四者实际已装
（volta、`~/.local/bin`、`~/.qoder-cn/entry`）。前一次修复（§6，commit 7d6dc5a）只修正了
二进制名映射（qodercli→qodercn）与认证提示，探测仍走裸 `shutil.which`。而 console 与
controller 均以 LaunchAgent 常驻，plist PATH 精简为
`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`，不含用户级安装目录——
这正是 opencode/pi（homebrew）显示正常、其余四个误判的原因。同一探测语义当时在仓库里有
3 处独立实现：`console/herdr_factory_console.py:preflight`、`herdr/preflight.py:inspect`、
`herdr/deep_preflight.py:resolve_binary`（第三处有 zsh 兜底但硬编码目录漏了 volta）。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| `shutil.which` 在 LaunchAgent 里漏判 | 服务进程 PATH ≠ 用户 shell PATH；`~/.zshrc` 补的 PATH 对常驻服务不可见 | 二进制解析不得裸依赖进程 PATH，必须有目录兜底或登录 shell 兜底 |
| AGENT_BINARIES 映射 3 处手写 | 与 §6/§7 同类：同一语义多处手写必然漂移（qodercli 映射 bug 即由此而来） | 映射与解析收敛到 `herdr/agent_binary.py` 单一事实来源，消费方只 import |
| 修复"看起来改了"但问题复现 | 第一次修复只覆盖了名字映射这一层，未追问 `which` 本身的适用边界 | 修 bug 时先完整走一遍数据链路（UI 字段 → 判定函数 → 执行环境），确认根因层而不是症状层 |

### 操作规范

1. 任何需要定位 Agent CLI 的代码，一律 `from herdr.agent_binary import resolve_agent_binary`；
   解析顺序：`shutil.which` → `EXTRA_BIN_DIRS`（`~/.local/bin`、`~/.volta/bin`、`~/.qoder-cn/entry`、homebrew）→ 登录 shell `command -v`。
2. 新增 Agent 注册入口收敛到 `herdr/agent_binary.py:AGENT_BINARIES`；
   `herdr/preflight.py` 只维护 `KNOWN_AGENTS`/`AUTH_HINTS`/`VERSION_ARGS`，`deep_preflight.py` 只维护 `AUTH_HINTS`/错误模式。
3. 给 LaunchAgent 服务写依赖用户环境的功能前，先看 `~/Library/LaunchAgents/com.user.*.plist` 的
   `EnvironmentVariables.PATH`；需要用户 PATH 的逻辑放代码兜底，不要依赖改 plist。
4. 排查"服务里不对、终端里正常"类问题时，第一步用
   `env -i HOME=$HOME PATH=<plist PATH> <python> -c ...` 复现服务环境，再谈代码。

### 验证命令 / 证据

```bash
/opt/homebrew/bin/pytest tests/test_agent_binary_resolution.py tests/test_console_agent_roster.py
bash scripts/install-herdr-console.sh
curl -s "http://127.0.0.1:8765/api/project?id=nexusarchive-54433229" | python3 -c "import json,sys; [print(a['agent'],a['status'],a['binary']) for a in json.load(sys.stdin)['data']['agents']]"
env -i HOME=$HOME PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin python3 -c "import sys; sys.path.insert(0,'$HOME/herdr'); from herdr.agent_binary import resolve_agent_binary; print(resolve_agent_binary('codex'))"
```

实际修复证据：`/api/project` 返回六 Agent 全部 `ready`，binary 均为绝对路径
（codex/claude→`~/.volta/bin`，qodercli→`~/.qoder-cn/entry/qodercn`，agy→`~/.local/bin/agy`）；
最后一条命令模拟 LaunchAgent 精简 PATH，解析同样成功。

## 9. "清空"类机制的结构性失效:pane 复用从未发生,清理应做在生命周期终点而非复用入口

### 问题背景

工作流结束后每个阶段 tab 遗留 2+ pane,agent 上下文无限累积。系统里存在的
"清空设置"(dispatch 前向 pane 发送 `/clear`,`bin/herdr-task` dispatch_task)
每次派发都在执行,却从未产生过清空效果——因为它的设计前提是"复用旧 pane",
而 `herdr/pane_pool.py:_claimed_panes` 对 tasks.json 中所有带 pane_id 的任务
永久占用(不过滤状态、pane_id 永不释放),`acquire_pane_for_task` 永远找不到
可用 pane,于是每个任务都拿到全新 pane,`/clear` 每次都打在空白容器上。
同时 `herdr` 不持久化终端 scrollback(CLI 已无 `--source logfile`,旧引用失效),
pane 一关画面即失,销毁与证据天然冲突。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| /clear 每次执行却从未生效 | 清理机制挂在"复用入口"上,而复用通道被另一处代码结构性堵死——两个模块各自正确,组合起来是死代码 | 评审清理/重置类机制时,先验证它的**触发前提**在真实链路里是否成立,而不是只验证机制本身会执行 |
| 用 /clear 复用容器防上下文污染 | 清空旧容器永远清不干净(agent 私有命令、磁盘 session、scrollback 残留),这是幻觉温床 | 隔离靠"生新死灭"(新 pane + 新会话 + 用后销毁),不靠清空复用;跨任务只传固化产物 |
| pane/clone 保留被当成默认 | "保留现场"是显式例外(排障),销毁才是默认;默认保留会让上下文与磁盘单调膨胀 | 资源生命周期必须有终点:验收收敛 → 证据固化 → 销毁活体 |
| 关共享 tab 连带销毁其他 workflow 的 pane 且无证据 | 共享资源的批量销毁必须先验证归属,否则会误伤"不在本次操作范围内"的现场 | 破坏性批量操作前枚举受影响对象并校验所有权;无法枚举时放弃操作 |

### 操作规范

1. 任务收尾统一走 `herdr-task finalize <task_id>`(单任务)或
   `herdr-task close-workflow <wf>`(批量/自动);固定顺序:**转写 dump →
   pane close → 分档删 clone → 状态推进**,证据在
   `~/.herdr-controller/logs/tasks/<task_id>/`。
2. clone 删除前必须满足:有 `integration_ref`(已完成 integrate)或 `superseded`;
   `committed` 未 integrate 拒删;mode=none 任务需 `--purge-clones` 显式授权。
3. 关闭阶段 tab 前必须经 `_tab_foreign_panes` 校验归属;pane list 失败时
   宁可跳过不可盲关。
4. failed 任务现场默认保留(`--force` 才收);总指挥 pane 保留到知识沉淀
   与 PR 合并之后,用 `close-workflow --include-coordinator` 收口。
5. 禁止恢复"pane 复用 + 清空"路线:`_claimed_panes` 永久占用是隔离原则的
   执行机制;若未来引入复用,必须连同 per-agent clear 映射与 session 重启
   一起设计,并重开评审。

### 验证命令 / 证据

```bash
pytest tests/test_workflow_finalize.py tests/test_stage_advance_and_supersede.py
herdr-task close-workflow wf-nexusarchive-54433229-20260912-232500 --dry-run
herdr-task close-workflow wf-nexusarchive-54433229-20260912-232500
```

实际收尾证据:15 份 `~/.herdr-controller/logs/tasks/nx09122325-*/terminal.log` 落盘;
5 个 clone 删除(3 集成 + 2 superseded)、10 个 docs clone 按安全档保留;
wA 现场 24 pane/7 tab → 1 pane(总指挥)/1 tab;controller 自动收尾使
9/9 历史 workflow 到达 `completed`。决策全记录:
`docs/walkthroughs/20260913-workflow-finalize.md`。

---

## 10. 内嵌单行前端资源的三类暗雷：字号/圆角与间距同值、测试字符串锁、属性级正才可盲改

### 问题背景

Console 前端全部内嵌在 `console/herdr_factory_console.py` 的两个单行字符串里
（L447 CSS、L448 HTML/JS）。2026-09-13 按 snapping-ui-to-grid 技能做全量间距
白名单治理（36 处裸值）时暴露：盲替换 `14px→16px` 会误伤 `font-size:14px`；
`padding`/`gap`/`margin` 各自上下文不同值不同，同值替换会跨语义；同时
`tests/test_console_run_job.py` 等用 `assertIn` 把 JS 关键子串
（如 `state.opsMode?'← 返回工厂':'进入运维驾驶舱'`）钉死在源码上。

### 经验教训

| 教训 | 说明 |
|------|------|
| 单行 CSS 里同数值不同语义 | `14px` 同时是 padding 与 font-size；替换必须以"属性名+选择器"为锚，不能以数值为锚 |
| 测试断言是隐性 API | 源码字符串被 pytest `assertIn` 锁定的部分等价于对外契约，改前先 grep tests/ |
| 纪律门禁要可执行 | "间距只用 4/8/16/24/32"这类规范必须配直方图脚本/grep，否则必然回潮 |

### 操作规范

1. 改内嵌前端资源前先跑 `grep -n 'assertIn' tests/test_console_*.py` 列出字符串锁；
2. 数值替换一律用属性级锚定（含前后选择器片段），改完跑属性级直方图复核：
   `python3 -c` 提取 `(padding|margin|gap)(-[a-z]+)?:` 捕获组统计 px 值；
3. UI 规范类约定同步落到可执行 grep（技能自带命令或等价脚本），收尾必跑。

### 验证命令 / 证据

```bash
grep -n 'assertIn' tests/test_console_*.py
sed -n '447p' console/herdr_factory_console.py | \
  grep -E '(padding|margin|gap)[^:;}]*:[^;}]*[^0-9.](5|6|7|9|11|13|14)px'  # 期望零命中
pytest tests/test_console_run_job.py tests/test_console_templates.py \
  tests/test_console_view_state.py tests/test_console_agent_roster.py \
  tests/test_console_stage_summary.py   # 48 passed
```

决策全记录：`docs/walkthroughs/20260913-console-grid-alignment.md`。

## 11. Qoder 身份漂移第三幕：修复在 repo，病灶在外部工具层（herdr 集成装错产品目录）

### 问题背景

用户报告"agent 中的 qoder 不对，正确的是 qodercn，已经改过两次还没好"。
前两次修复（§6 commit 7d6dc5a、§8 commit 00d52ba）都在 repo 内收敛
qodercli→qodercn 的二进制映射（preflight/console 探测层），但 Qoder 系 agent
的会话上报从未成功过——`herdr agent list` 里 qodercli 条目始终没有
`agent_session`（claude/codex/opencode 都有）。全局排查发现病灶在 repo 之外：
homebrew `herdr` 工具（terminal workspace manager）的
`herdr integration install qodercli` 把 SessionStart 钩子硬编码装进**国际版
Qoder 产品目录** `~/.qoder`（其二进制 strings 仅含 `.qoder`，无 `.qoder-cn`），
而实际运行的是 Qoder CN CLI（`~/.local/bin/qoderclicn`，配置目录
`~/.qoder-cn`），读不到该钩子 → 上报链路自安装起就是断的。
本机并存两个不同产品：国际版 Qoder（`~/.qoder`，CLI 名 `qoder`）与 Qoder CN
（`~/.qoder-cn`，官方命令 `qodercn`，实际二进制 `qoderclicn-1.1.51`）；
`~/.local/bin/qodercli` 是人造 symlink 指回 CN entry。

### 经验教训

| 教训 | 说明 |
|------|------|
| 修复必须覆盖"真正执行的那一层" | repo 的 `herdr/agent_binary.py` 只管探测；拉起进程的是外部 herdr 工具内置 kind 表，会话上报靠 CLI 产品目录里的钩子。只改 repo 永远碰不到病灶 |
| 工具层别名把两个不同产品并成一个 | herdr 检测 manifest `aliases=["qoderclicn","qoder","qodercn"]` 把国际版 qoder 混为同一 agent；安装器硬编码 `~/.qoder`。产品级区分必须在工具层显式纠正（本地 manifest 覆盖） |
| CLI 钩子有"目录信任"门禁 | QoderCN CLI 对钩子报 `Security: Blocked execution of hook (user) in untrusted folder`：cwd 不在 `permissions.trustDirectories`（默认 `["/Users/user"]`）内则 SessionStart 钩子一律不执行。在 /tmp 里验证必然假阴性；工厂克隆目录天然受信任 |

### 操作规范

1. 排查 agent 身份类问题按五层取证：repo 映射（`herdr/agent_binary.py`）→
   运行时状态（`~/.herdr-controller/*.json`）→ 拉起层（herdr 工具 kind/检测
   manifest）→ CLI 产品配置（`~/.qoder` vs `~/.qoder-cn`）→ 实际进程
   （`ps aux` + `ps eww` 看配置目录 env）。
2. QoderCN 的 herdr 集成以 `~/.qoder-cn` 为准；任何人再跑
   `herdr integration install qodercli` 会装回 `~/.qoder`，必须重做迁移
   （步骤见 walkthrough）。
3. 验证钩子必须在受信任目录内起真实 agent（`--cwd ~/HAFlow` 或
   `~/.herdr-controller/clones/*`），以
   `herdr agent list` 中 `agent_session.source=="herdr:qodercli"` 为准。
4. 检测别名收敛用本地覆盖 `~/.config/herdr/agent-detection/qodercli.toml`
   （local 永远 shadow remote；remote manifest 更新后需人工同步别名修正）。

### 验证命令 / 证据

```bash
herdr server agent-manifests   # qodercli: source_kind="local override", local_override_shadowing_remote=true
herdr agent explain wA:p2A     # manifest: /Users/user/.config/herdr/agent-detection/qodercli.toml
herdr agent list               # 修复后 qodercli 首次出现 agent_session（source=herdr:qodercli）
grep "herdr-agent-state" ~/.qoder-cn/logs/runs/<run>/qodercli.log  # hook.started 记录
```

决策全记录：`docs/walkthroughs/20260913-qodercn-agent-identity-fix.md`。

## 12. 流程完成 ≠ 交付完成:质量门的"不通过"必须驱动结构回流,而非归档

### 问题背景

wf-nexusarchive-…-084418 全流程走完:评审产出 B1(P0)阻断结论,但结论只存在
于自然语言报告——引擎 `is_node_complete` 只看任务状态,照常推进 wrapup 并将
交付被阻断(PR 禁合)的 workflow 归档 `completed`。修复只能在新的孤立
workflow 里另起炉灶,丢失 PR 关联与阶段历史。审查轮 1(对抗性)进一步发现
初版设计三处结构漏洞:verdict 死循环(作废范围漏 gate 自身)、重测缺失
(下游闭包不完整)、reopen 自消除(sweep 会把重开的 workflow 秒回 closed)。

### 经验教训

| 教训 | 说明 |
|------|------|
| 完成态判定与结论语义是两层 | 任务"完成"只证明交付物存在;pass/blocked 是另一维状态。质量门的结论必须有机器可读载体并被推进逻辑消费,否则最强质量信号被浪费 |
| 回流的正确粒度是"retry_node 全部下游" | 只回炉 gate 自身会死循环(旧 verdict 残留),只回炉 gate 不回炉下游会跳过重测。作废闭包必须覆盖 gate+全部下游 |
| 结构性消除竞态优于防线叠加 | "节点未完成"本身就是闩(作废后周期 sweep 打不穿),不需要额外锁位;给 gate 打 notified 反而会被 reconcile 的前置依赖判定卡成永久停摆 |
| reopen 类"复活"操作自带自消除竞态 | 旧状态仍满足终态判定时,下一个周期事件就会把它再次终结。需要显式闩 + 明确的摘除时机(首个活跃任务) |
| 独立审查要给对抗性清单,并复核其建议 | 审查抓到 3 个设计级漏洞,但也给出 1 个会造成永久停摆的修法(用回归测试证伪后拒绝) |

### 操作规范

1. 门禁阶段验收必须落 verdict:`herdr-task set <t> completed --verdict
   pass|blocked --note`(blocked 必填 note);
2. blocked 的恢复路径:Controller 自动作废 gate+下游 → 总指挥按 fix_loop
   事件派发 fix task(`--onto` 落 PR 分支)→ DAG 自动重流;禁止新建
   workflow、禁止放弃;
3. 放弃交付必须显式:`close-workflow --abandon`(outcome=abandoned),
   console 的候选分支合并/手工推进遇 blocked verdict 一律拒绝;
4. 复用已关闭 workflow:`reopen-workflow`,首个任务派发前闩保护。

### 验证命令 / 证据

- `/opt/homebrew/bin/pytest tests/`(183 passed,含 fix-loop 33 用例);
- 设计与审查记录:`docs/walkthroughs/20260913-fix-loop-design.md`(§8 审查修订);
- 知识同步:`wiki/task-lifecycle.md` §1.1、`wiki/dag-workflow-engine.md` §10。

## 13. 幽灵推进：close-workflow 关不掉 controller 的推进循环（终态必须在循环处执法）

### 问题背景

用户重复提交同一需求产生两个工作流，关闭重复项 wf-…-111426（零任务）后，
controller 继续对其逐阶段"真空推进"：零任务工作流对 `is_node_complete` 真空
成立，legacy stages 分支又没有整体完成检查，于是 requirements→plan→
implementation 无限排队；stage_advance 消费线程在协调者 idle 时把幽灵提示
`herdr agent prompt` 直注**共享协调者 Pane**，协调者照办派发了 3 个真实任务
（含一个与活跃工作流 111049 正式实现任务完全重复的 codex 实现任务）。
根因链：`active_registered_workflows()` 名为 active 实则返回全部注册表条目；
`check_workflow_stage_advance` 只查 `startup_ready` 不查终态；全系统唯一的
status 检查只用于防"重复关闭"。

### 经验教训

| 教训 | 说明 |
|---|---|
| 终态必须在消费循环处执法 | 注册表写终态 ≠ 引擎停手；凡按 id 扫描/派发的循环都要自查终态，且消费线程 fire 前要**逐迭代**再校验（事件可在队列里存活分钟级，入队时合法 ≠ fire 时合法） |
| 零任务工作流是推进引擎的退化用例 | `is_node_complete` 对空集真空成立；任何"全部完成则推进"的逻辑都必须先回答"空集算完成吗" |
| 名实不符的函数是事故温床 | `active_registered_workflows()` 返回的是**全部**条目；名字承诺与实现不符时，要么改实现要么改名，留着眼就是给下一个读者埋雷 |
| 双保险要落在不同层 | 扫描侧过滤（不产新事件）+ 消费侧再校验（丢已入队事件）缺一不可；只堵源头堵不住已上膛的子弹 |

### 操作规范

1. 终态判据语义（`status=="completed"` 即终态、缺 status 视为活跃）在
   `herdr/projects.py` 共享谓词（factory 侧消费）与 controller 内联实现
   （本地 `workflow_closed` + sweep 过滤）各有一份——修改口径必须两处同步，
   并补 `tests/test_workflow_registry_guards.py` 用例；
2. 关闭无任务工作流后，若 controller 仍打印其 `STAGE ADVANCE` 行，说明终态
   闸门失效，按 `docs/walkthroughs/20260913-controller-ghost-advance-and-create-guard.md`
   §3 配方处置（移除注册表条目 → pending 事件经 no-coordinator-pane 自毁）；
3. 同项目默认串行：`herdr-factory run` 遇活跃工作流直接拒绝（exit 2），
   `--force` 是唯一逃生口且不暴露给 Console。

### 验证命令 / 证据

```bash
# 闸门拒绝（111049 活跃时）
herdr-factory run --project /Users/user/nexusarchive "guard test"; echo $?  # 期望 exit 2 + 拒绝消息
# 幽灵消除：零任务工作流被干净自动关闭而非循环推进
grep -c "STAGE ADVANCE.*125332" ~/.herdr-controller/logs/controller.out.log  # 期望 0
决策与会话碰撞全记录：`docs/walkthroughs/20260913-controller-ghost-advance-and-create-guard.md`。

## 14. 内嵌前端代码转义暗雷与无头语法盲区：Python 字符串转义击穿 JS 导致全屏白屏

### 问题背景

2026-09-13 交付工作流短 ID 与任务名称（`feat/workflow-title-and-short-id`）时，为 Console 前端
弹窗添加任务名称输入框及失焦自动提炼标题逻辑。因 `console/herdr_factory_console.py` 的
`HTML_TEMPLATE` 使用了标准 Python 多行字符串（`'''...'''` 而非 `r'''...'''`），新增的 JS 正则
与换行分割逻辑中的 `\n` 被 Python 评估为物理换行字节（`0x0A`）。前端加载时 JS 引擎抛出
`Uncaught SyntaxError: Invalid regular expression: missing /`，导致整个页面 JS 执行链在初始化阶段
彻底熔断，`refreshAll()` 未能执行，用户界面呈现为"没有任何数据"的全白屏。
当时后端的 193 项 pytest 全部绿灯，暴露出测试套件对前端内嵌脚本的语法守卫盲区。

### 经验教训

| 教训 | 说明 |
|---|---|
| 内嵌多行模板必须声明为 Raw String | 任何在 Python 中编写的内嵌 HTML/JS，首行必须为 `r'''` 或 `r"""`，严禁让 Python 解析器对 JS 正则与转义符进行二次解释 |
| 后端测试通过 ≠ 前端没有语法崩溃 | 后端单测仅覆盖了 Python HTTP Handler 和 API 数据结构，无法替代前端内嵌 `<script>` 的解析执行验证 |
| 跨语言内嵌代码必须有语法静态门禁 | 单文件 Python-JS 架构必须在 CI/pytest 阶段抽取 inline script 并运行 `node -c`，将语法错误扼杀在提交前 |

### 操作规范（已固化到 `tests/test_console_frontend_syntax.py`）

1. **模板前缀强制约束**：`console/herdr_factory_console.py` 中的 `HTML_TEMPLATE` 必须使用 `r'''` 或 `r"""` 声明，禁止回退；
2. **自动化语法门禁**：新增 `tests/test_console_frontend_syntax.py`：
   - 静态检查 `HTML_TEMPLATE` 源码是否包含 `r'''` / `r"""`；
   - 提取 `<script>` 完整代码块调用 `node -c` 运行静态语法编译检查；
   - 断言弹窗表单核心 DOM 结构与函数钩子（`newTitle`, `autoFillWorkflowTitle()` 等）；
3. **前端修改交付规范**：修改内嵌前端后，必须先执行 `pytest tests/test_console_frontend_syntax.py`，再同步至 `~/.herdr-console`。

### 验证命令 / 证据

```bash
# 门禁测试：验证模板 Raw String 声明及 JS 语法干净度
pytest tests/test_console_frontend_syntax.py  # 期望 4 passed
# 生产脚本无报错验证
node -c /tmp/check_syntax.js                  # 期望 exit 0
```

---

## 15. 终端屏幕旧完成标记残留击穿 rework 状态陷阱（非派发态严禁判定 agent_done）

### 问题背景

在 `wf-nexusarchive-54433229-20260913-111049` 中，评审任务 `review-01-superadmin-quality` 发现实现存在 P1 角色漏判。评审 Agent 完成分析并在屏幕输出 `HERDR_TASK_DONE:review-01-superadmin-quality` 后退出。
总指挥误将该评审任务设为 `rework`。巡检守护进程 `services/herdr-sentinel.py` 周期性读取 Pane 终端屏幕，因终端历史缓冲区依然保留上一次执行留下的 `HERDR_TASK_DONE` 文本，Sentinel 误判为“Agent 已重新执行完成”，在 3 秒内自动触发：
`[SENTINEL STATE] review-01-superadmin-quality: rework -> agent_done (completion_sentinel)`
并将 Controller 重启。总指挥陷入重复通知与判断死循环，Controller 大量报 `[COORDINATOR BUSY]`。

### 经验教训

| 教训 | 说明 |
|---|---|
| 屏幕回显不可逆 | 终端屏幕（TTY/Pane）是有状态的滚动缓冲区；Agent 进程退出后历史文本依然可见，不能直接当作新一轮执行的凭证 |
| 状态转移前置条件必须严格封闭 | 只有处于 `dispatched` 或 `working`（即真正被下发了新 Prompt 且处于运行期）的任务，屏幕上的完成标记才合法；`rework` 或 `blocked` 态在被重新下发前绝对不可直接变 `agent_done` |
| 阶段评审打回 ≠ 评审任务自身 rework | 评审发现被审代码有 bug，评审任务自身是成功的（完成职能）；应该打 `verdict: blocked` 驱动回流，而不是把评审任务自身设为 `rework` |

### 操作规范

1. **Sentinel 判定守卫（已固化到 `services/herdr-sentinel.py`）**：
   必须限定 `status in {"dispatched", "working"}` 时方可扫描屏幕 `HERDR_TASK_DONE` 标记，严禁在 `rework` 或 `blocked` 态未重新派发前判定完成；
2. **总指挥验收指引（已固化到 `services/herdr-controller.py`）**：
   门禁评审/测试任务发现代码缺陷，必须执行 `herdr-task set <id> completed --verdict blocked --note "<blocker>"`，严禁对评审任务使用 `rework`；
3. **引入微循环环境**：使用 `.herdr-loop` 配合 `herdr-task verify-metrics` 进行量化评分门禁验收。

### 验证命令 / 证据

```bash
# 全量测试套件（含微循环与外部流 208 用例）
pytest tests/  # 期望 208 passed

# 真实事故日志证据
grep "rework -> agent_done" ~/.herdr-controller/logs/sentinel.out.log:75
```

---

## 16. 空间底座与工厂车间的概念断层：未接入空间的负向死胡同与控制台创建闭环缺失

### 问题背景

用户在 Herdr 终端多路复用底座中自由开辟新 Space（如 `wC` 只有 1 个工位），进入共事工厂控制台后显示为红色的"未注册"，右侧看板全灰禁用并提示"该空间仅展示，不参与当前自动工作流调度"。用户发现陷入死胡同：
1. 若在 Herdr 建空间：无法自动应用工厂标准的阶段节点模板（1总指挥 + 6阶段Tab + Anchor）；
2. 若在工厂控制台建：控制台界面只有"＋ 新需求"（针对已有项目），完全没有"＋ 新建工厂空间"入口，也没有为未接入终端提供"注册/装配"按钮，造成严重的体验割裂与认知焦虑。

### 经验教训

| 教训 | 说明 |
|---|---|
| 容器底座与业务车间必须分层明晰 | Herdr 是终端进程容器（无感知 DAG 与模板）；Factory 是流水线调度器。凡需套用模板的流水线，产品入口必须由工厂统一装配与发起 |
| 永远不要给用户只抛出负向禁令而不给正向转化出口 | 当检测到"未注册/未接入"空间时，不能只提示"不参与调度"，必须就地提供"一键按模板装配为工厂空间"的操作卡片，打通闭环 |
| 标签文案切忌制造系统故障的假象 | 外部普通终端不是系统错误或未授权，不可使用强烈的红色"未注册"恐吓用户；应使用中性的"独立终端"或"未接入"，并明确引导装配 |
| 生命周期闭环：有注册必有安全注销 | 接入项目后必须提供注销/解绑入口。注销守卫严禁触碰本地代码，必须拦截活跃任务防意外中断，并支持终端空间弹性保留/关闭 |

### 操作规范

1. **新建车间入口统一收敛**：Console 侧边栏常驻【＋ 新建工厂空间】，用户提供本地 Git 路径与模板后，自动调用底层 `herdr/projects.py:create_project` 创建 Workspace 并装配好全部节点工位；
2. **已有终端空间原地装配**：针对未接入的普通终端空间，Console 右侧看板渲染装配引导卡片，调用 `herdr/projects.py:adopt_workspace_as_project` 就地复用终端并补齐缺失的阶段 Tab 和 Anchor 锚点；
3. **安全注销守卫**：实现 `herdr/projects.py:unregister_project`，校验是否有活跃任务（未终态则拒绝，`--force` 显式放行）；Console 与 CLI（`herdr-factory unregister`）均提供注销操作，弹窗明确告知“代码绝对不删”，默认保留终端空间为【独立终端】，可选一键关闭空间窗口；
4. **CLI 与 Web 端契约对齐**：`herdr-factory project --workspace <wid>` 保持与 Web `POST /api/project/adopt` 对齐，`herdr-factory unregister` 与 `POST /api/project/unregister` 对齐。

### 验证命令 / 证据

```bash
# 控制台项目创建与空间装配测试
pytest tests/test_console_project_creation.py

# 项目安全注销与状态拦截测试
pytest tests/test_project_unregister.py

# CLI 帮助与选项验证
herdr-factory project --help     # 包含 --workspace 与 --template
herdr-factory unregister --help  # 包含 --project, --close-workspace, --force
```

---

## 17. 全生命周期状态机完备性与防抖解耦陷阱（严禁凭瞬间 idle 抢报完成、解耦 Sentinel 强杀）

### 问题背景

在 `wf-herdr-0913-01` 推进至 `plan` 阶段时，连续暴露出三个危及业务稳定性的底层隐患：
1. **Agent 推理间歇瞬间 idle 导致误报完成**：qodercli / codex 在执行长耗时推理或子进程命令时，底层 Socket 偶发 1~2 秒短暂 `idle`，Controller 毫无防备地直接把任务置为 `agent_done` 并通知总指挥验收，导致总指挥被打扰并产生误报打回；
2. **Sentinel 强杀 Controller 截断空中长交互**：Sentinel 每次在屏幕检测到完成标记更新 tasks.json 后，粗暴调用 `launchctl kickstart -k ...herdr-controller` 强杀 Controller，若此时 Controller 正在进行 `herdr agent prompt` 长等待，会被硬生生掐断造成死锁；
3. **状态机 TRANSITIONS 僵硬限制**：任务极速完成或返工完成后，若从 `dispatched` 或 `rework` 直接 set `agent_done`，因不在合法集合中抛出 `Invalid transition` 导致流程直接卡死；且系统缺乏任务和工作流的显式 `paused` 暂停机制。

### 经验教训

| 教训 | 说明 |
|---|---|
| 严禁单凭终端 socket 瞬间 idle 判定任务完成 | 大模型生成、网络等待、子进程编译均可能出现毫秒级/秒级无输出窗口；完成判定必须有终端显式完成标记或防抖二次确认 |
| 跨进程状态传递严禁依赖强杀被通知方 | 守护进程间通信应基于数据落盘 + 主动巡检轮询（pull model），绝不能通过杀进程（restart）来“强行唤醒”，否则必然斩断空中长耗时交互 |
| 状态机转移表必须覆盖全部敏捷与重做通路 | 实际业务中返工（rework）和极速完成（dispatched->agent_done）是常态，状态机必须对返工完成、遇阻解决有合法的收敛闭环 |
| 生产级工作流引擎必须具备暂停/恢复安全开关 | 当遇到外部环境维护或人工复核时，必须提供原子 pause / resume，绝不能靠置空配置或杀进程来临时刹车 |

### 操作规范

1. **完成判定双重门禁**：在 `services/herdr-controller.py:handle_event` 中，当收到 `idle` 事件时，优先核查终端屏幕是否存在 `HERDR_TASK_DONE:{task_id}` 标记；若无标记，执行 2 秒防抖探测，确认 Agent 是否恢复 `working`，彻底过滤推理间歇抖动；
2. **解除 Sentinel 强杀**：`services/herdr-sentinel.py` 扫描到完成标记后仅安全原子更新 tasks.json；由 Controller 的 `registry_watcher` 在周期扫描中主动捞取未入队的 `agent_done` 任务并推入协调器队列；
3. **加固状态机 TRANSITIONS**：在 `bin/herdr-task` 中允许 `dispatched -> agent_done`、`rework -> agent_done`、`blocked -> agent_done`，以及 `paused -> working / rework / superseded`；
4. **工作流与任务级暂停支持**：提供 `herdr-task pause / resume <task_id>` 与 `herdr-factory pause / resume <workflow_id>`，在 `check_workflow_stage_advance` 中拦截暂停中的工作流。

### 验证命令 / 证据

```bash
# 全生命周期矩阵测试（覆盖极速完成、返工循环、暂停恢复、并行门禁、Watcher主动捞取、防抖过滤）
pytest tests/test_workflow_lifecycle_matrix.py -v

# 全仓自动化回归（234 个用例全部通过）
pytest tests/

# CLI 暂停与恢复命令验证
bin/herdr-task pause <task_id>
bin/herdr-task resume <task_id>
bin/herdr-factory pause <workflow_id>
bin/herdr-factory resume <workflow_id>
```


---

## 18. Stale 运行时状态击穿回炉状态陷阱（reconcile 不能用外部瞬态覆盖持久化意图态）

### 问题背景

任务 `111049` 触发 `rework`（总指挥裁决打回重做）后，Controller 的 `reconcile_task_state` 在下一个周期扫到该 Pane 运行时仍报 `done`（上一轮进程残留），立即将任务从 `rework` 提升为 `agent_done`，绕过了总指挥的回炉裁决，导致工作流错误推进到下一阶段。

**关键误解**：`reconcile` 的原始设计意图是"如果运行时已完成而调度状态落后，就同步"。但 `rework` 是持久化的**意图态**（由人/总指挥主动写入），不是可以被运行时快照覆盖的派发态。

### 经验教训

1. **持久化意图态 vs. 运行时快照态必须分层**：`pending / dispatched / working` 等由 Controller 自动推进的态，可以被运行时快照同步；`rework / paused / blocked` 等由人工/总指挥裁决写入的态，属于意图态，**严禁**被来自外部运行时的瞬态信号覆盖。
2. **Reconcile 的准入白名单原则**：`reconcile_task_state` 只允许在明确的"派发中但运行时已先完成"场景下触发状态提升，任何非 `dispatched/working` 的源态都应直接 skip。
3. **Stale 进程问题的结构根因**：运行时报告 `done` 不代表当前任务完成——可能是旧进程残留、Pane 复用遗留、或调度竞态。必须结合任务的 `started_at` 时间戳与进程 PID 做二次确认，而非单纯信任 `done` 快照。

### 操作规范（已固化到 `services/herdr-controller.py`）

```python
# reconcile_task_state 的防御写法（PR #7 修复）
def reconcile_task_state(task, runtime_status):
    current = task.get("status")
    # 意图态白名单：仅允许从派发态提升，回炉/暂停态绝对不允许被运行时覆盖
    RECONCILE_ALLOWED_SOURCES = {"dispatched", "working"}
    if current not in RECONCILE_ALLOWED_SOURCES:
        return  # 意图态，跳过 reconcile
    if runtime_status == "done":
        advance_to_agent_done(task)
```

### 验证命令 / 证据

```bash
# 精准回归：确认 rework 任务在运行时报 done 时不被翻转
pytest tests/test_fix_loop_anti_flapping.py::ControllerReconcileReworkTest -v

# Git 证据
# PR #7 commit c263211
# services/herdr-controller.py: reconcile_task_state 防御补丁
```

### 相关文档 / 关联证据

- PR #7: https://github.com/allinai0506/Cowork/pull/7
- 修复文件: `services/herdr-controller.py`（`reconcile_task_state` 函数）
- 测试文件: `tests/test_fix_loop_anti_flapping.py`

---

## 19. Fire-and-Forget 工位的系统性质量失控：Agent 必须有 Inner Loop 强制自验才能保证交付质量

### 问题背景

工位 Agent 在没有强制自检约束的情况下，会在完成代码编写后立即输出 `HERDR_TASK_DONE`，跳过测试运行与质量验证。这导致：①总指挥收到"完成"信号后进行宏观验收时发现代码有明显错误；②修复责任回流给总指挥，总指挥被迫盯微观细节（违反双环分工）；③Fix-Loop 频繁触发，系统进入高频返工震荡。

**根本缺陷**：系统缺少"工位不得在自检通过前交卷"的结构性约束。

### 经验教训

1. **Agent 系统的"完成"不等于"质量达标"**：输出完成标记是一个行为，通过自检是一个质量门。没有强制绑定两者的机制，Agent 会自然地走最短路径——直接完成，不验证。
2. **铁律必须在系统层面注入，不能依赖 Agent 的自觉**：Prompt 里的"建议"没有强制力，必须通过可量化的评估工具（`herdr-loop eval`）+ 明确的行为约束（禁止在得分 < 100.0 时输出完成标记）构成结构性约束。
3. **熔断路径必须有明确协议**：当工位无法自愈时，必须有标准化的"求助信号"（`HERDR_TASK_BLOCKER`）让调度层感知，而不是静默卡死或直接失败。这条路径的缺失会导致 Agent 要么死循环自愈，要么直接 bail out 交出不完整的产物。
4. **双环分工的工程保障**：总指挥只做宏观验收的前提是工位已经完成了微观自验。没有 Inner Loop 机制，双环就是空谈。

### 操作规范（已固化到 `bin/herdr-task`、`herdr/evaluator.py`、`services/herdr-sentinel.py`）

1. **启动时装配 `.herdr-loop`**：`auto_init_task_loop()` 在任务 Clone 目录生成 `GOAL.md` + `EVALUATOR.sh` + `STATE.md`；
2. **Prompt 铁律注入**：`dispatch_task()` 在 `.herdr-loop` 存在时注入三条铁律（禁止未通过自检就交卷、禁止放弃工位、禁止上报局部问题）；
3. **熔断求助协议**：工位达到 `max_iterations` 时，`herdr-loop eval` 自动生成 `BLOCKER.md`，Agent 输出 `HERDR_TASK_BLOCKER:<task_id>`，Sentinel 检测后将任务转为 `blocked`（reason: `inner_loop_exhausted`），路由给总指挥仲裁；
4. **Sentinel 感知 BLOCKER 信号**：与感知 `HERDR_TASK_DONE` 平级的信号通道，保证熔断路径有调度层支撑。

### 验证命令 / 证据

```bash
# Inner Loop 协议回归（15 个用例）
pytest tests/test_inner_loop_protocol.py -v

# BLOCKER.md 生成验证
python -c "
from herdr.evaluator import generate_blocker_report, MetricVector, LOOP_DIR_NAME
from pathlib import Path
import tempfile, json
with tempfile.TemporaryDirectory() as d:
    loop_dir = Path(d) / LOOP_DIR_NAME
    loop_dir.mkdir()
    m = MetricVector(composite_score=55.0, failing_tests=['test_x'], lint_errors=1)
    f = generate_blocker_report(loop_dir, m, iteration=3, max_iter=3)
    print(f.read_text())
"

# Git 证据
# PR #7 commit c263211
```

### 相关文档 / 关联证据

- PR #7: https://github.com/allinai0506/Cowork/pull/7
- 实现文件: `bin/herdr-task`（loop_suffix 铁律注入）、`herdr/evaluator.py`（`generate_blocker_report`）、`services/herdr-sentinel.py`（BLOCKER 信号检测）
- 测试文件: `tests/test_inner_loop_protocol.py`
- 设计文档: `docs/walkthroughs/北极星架构体系：通用人机协同运行时 (Universal Human-Agent Collaborative Runtime).md`

---

## 20. macOS CLI 通知宿主归属与点击跳转断裂：osascript 误归属脚本编辑器，需 Deep-Link 与 terminal-notifier 闭环

### 问题背景

`services/herdr-notifier.py` 负责在任务状态发生改变（`blocked`, `failed`, `human_review`）或工作流完成时发出 macOS 原生系统通知。早期实现直接使用 `/usr/bin/osascript -e 'display notification ...'`。
**故障现象**：
1. **宿主归属错误**：macOS 将通过 CLI 调用的 `osascript` 默认归属为“脚本编辑器（Script Editor.app）”。用户点击通知横幅时，系统强行激活脚本编辑器，由于缺少上下文，要么报错“无法打开”，要么打开一个空白脚本窗口。
2. **动作跳转断裂**：AppleScript 原生 `display notification` 语法不支持携带 URL 或点击回调；而 Web 控制台（`console/herdr_factory_console.py`）只从 `localStorage` 读取状态，缺乏根据 URL 查询参数自动定位工作流和聚焦任务的能力。

### 经验教训

1. **CLI 系统通知必须有明确的点击目标与语义落地点**：通知的目的不仅是告知状态，更关键的是让用户一键进入问题现场。无跳转通道的通知在复杂的后台多 Agent 协同体系中会演变成阻断性噪点。
2. **AppleScript 与现代 macOS 通知系统的结构性代差**：`osascript` 仅适合极简提示，无法承担带 Deep-Link 调度的现代通知需求。必须在架构中优先采用支持 `-open <url>` 的原生工具（如 `terminal-notifier`），同时保留平滑降级（Graceful Degradation）以保障向后兼容。
3. **前端控制台必须支持外部 Deep-Link 传参**：控制台不能仅依赖内部状态管理或本地存储记忆；必须实现 URL 查询参数（`workflow_id`, `task_id`, `pane_id`）作为一等公民，形成“通知发出 -> 点击唤起浏览器 -> 控制台自动路由 -> 任务卡片高亮并弹窗”的完整闭环。

### 操作规范（已固化到 `services/herdr-notifier.py`、`console/herdr_factory_console.py`）

1. **通知工具双模分流与降级**：
   - 使用 `shutil.which("terminal-notifier")` 探测环境；
   - 存在时使用 `terminal-notifier -title ... -subtitle ... -message ... -open <deep_link_url> -sound Glass`；
   - 缺失时回退到 `osascript`，并在日志中输出一次友好安装指引（`brew install terminal-notifier`）。
2. **控制台 URL 路由契约**：
   - 启动初始化解析 `window.location.search`；
   - `workflow_id` 自动定位目标工作流与项目空间；
   - `task_id` 自动滚动到对应卡片、挂载 `.task-highlight` 样式并唤起 `showTask()` 弹窗。

### 验证命令 / 证据

```bash
# 1. 运行通知服务与控制台深链接自动化测试（含 mock 与 fallback 验证）
python3 -m unittest tests/test_herdr_notifier.py tests/test_console_deep_link.py

# 2. 控制台 JS 语法与 view-state 回归
python3 -m unittest tests/test_console_frontend_syntax.py tests/test_console_view_state.py

# 3. 发送测试通知验证 fallback 与 URL 构建
python3 services/herdr-notifier.py --test
```

### 相关文档 / 关联证据

- 实现文件：[`services/herdr-notifier.py`](file:///Users/user/HAFlow/services/herdr-notifier.py)、[`console/herdr_factory_console.py`](file:///Users/user/HAFlow/console/herdr_factory_console.py)
- 测试文件：[`tests/test_herdr_notifier.py`](file:///Users/user/HAFlow/tests/test_herdr_notifier.py)、[`tests/test_console_deep_link.py`](file:///Users/user/HAFlow/tests/test_console_deep_link.py)
- 知识库演进记录：[`wiki/architecture.md`](file:///Users/user/HAFlow/wiki/architecture.md)、[`wiki/log.md`](file:///Users/user/HAFlow/wiki/log.md)

---

## 21. 控制台内嵌 HTML/前端交互重构的无侵入原则与字面量契约保护

### 问题背景

在对单文件 Python HTTP Server（`console/herdr_factory_console.py`）进行 UI/UX 深度改造、可访问性（a11y）增强和交互现代化的过程中，为指标卡标签与选择器添加了辅助属性（例如 `<span id="labelProjects">项目空间</span>`、`<label for="wfSelect">工作流</label>`）。
导致既有的术语翻译契约测试 `tests/test_console_templates.py::TestTermTranslation` 失败。该测试直接断言无属性字面量 `self.assertIn("<span>项目空间</span>", self.html)` 与 `self.assertIn("<label>工作流</label>", self.html)`。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 直接给内嵌无属性 HTML 标签添加 id/for 属性 | 破坏了既有白盒测试对特定 HTML 片段的字面量断言 | 涉及测试断言的目标标签，必须保留原有字符结构；通过层次选择器（如 `.metrics .metric span`）获取 DOM |
| 交互阻塞（原生 window.confirm/prompt） | 破坏单页沉浸体验且在无头/受限环境可能被静默拦截 | 统一封装自定义非阻塞原生模态框（如 `showConfirmModal` / `showPromptModal`） |
| 外部未安装 npm/bundler 导致前端开发易引入重度依赖 | 违反 RULES.md 零依赖与离线开箱即用底线 | 坚持纯 CSS/SVG/Vanilla JS，图标全部内联轻量 SVG 矢量替代 Emoji |

### 操作规范（已固化到 `console/herdr_factory_console.py`）

1. **DOM 选择器与模板解耦**：
   - 动态更新文案时，优先采用 `.querySelectorAll('.metrics .metric span')` 索引获取，不强行注入 `id` 属性改动原始模板结构。
   - 保留 `<span>项目空间</span>`、`<span>活跃工作流</span>`、`<label>工作流</label>` 等契约片段原样输出。
2. **非阻塞交互替代原生弹窗**：
   - 彻底禁用 `window.confirm` 和 `window.prompt`；
   - 统一使用 `showConfirmModal({title, message, confirmText, danger, onConfirm})` 与 `showPromptModal({title, label, defaultValue, onConfirm})`。
3. **可访问性与键盘导航标准**：
   - 所有 Modal 容器显式标注 `role="dialog" aria-modal="true" aria-labelledby="..."`；
   - 全局注册 `Escape` 键盘事件，统一收起活跃模态框与展开的浮层下拉菜单。

### 验证命令 / 证据

```bash
# 1. 运行全部控制台相关单元与契约测试
/opt/homebrew/bin/pytest tests/test_console*.py

# 2. 全量回归测试保证零副作用
/opt/homebrew/bin/pytest

# 3. 部署并验证运行时状态
./scripts/install-herdr-console.sh
curl -s http://127.0.0.1:8765/ | head -n 10
```

---

## 22. 复杂多 Agent 调度内核的外部受控原则：控制元语与状态快照必须原子解耦，严禁闭门单向推进

### 问题背景

在早期调度器（`herdr-controller.py`）设计中，调度引擎采用“状态扫描即推进（Sweep-and-Advance）”的单向死循环机制。当总指挥或工位 Agent 发生方向偏差、依赖错误或需要人工干预调试时，缺乏结构化的底层指令支撑：
1. 外部无法原子化挂起或恢复特定节点；
2. 缺乏“单步执行（step）”机制，无法在断点处观察中间产物；
3. 一旦后续节点执行出错，缺乏拓扑回溯（rollback）与级联状态重置机制，导致历史脏任务与残留锁污染后续调度；
4. 门禁被外部误判卡死时，缺少可审计的强行放行（force_pass）通路；
5. 缺乏轻量、确定性的持久化检查点（checkpoint snapshot），无法安全试错或时间旅行恢复。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 调度器缺乏全局受控元语 | “自主单向推进”容易在遇到死循环或逻辑分支偏离时滚雪球式蔓延破坏现场 | 内核必须暴露 pause, resume, step, rollback, force_pass 一级控制元语 |
| 回溯清理不彻底导致幽灵任务 | 仅作废单一任务而忽略下游依赖拓扑，会导致下游任务处于挂起或孤儿状态 | 利用 Kahn 拓扑算法计算目标节点及其全部下游传递闭包，原子级联作废任务并重置节点调度锁 |
| 门禁人工特批绕过无审计 | 粗暴手动修改 JSON 状态破坏数据一致性且无法追溯谁在何时特批了什么 | 统一通过 `force_pass_gate` 注入 `[FORCE PASS by <operator>] <note>`，留存完整审计日志 |
| 状态快照未隔离存储 | 运行时内存瞬态容易在进程重启后丢失，无法支撑事后排障与状态回退 | 采用原子临时文件写入 `checkpoints/<workflow_id>/<cp_id>.json`，保存完整工作流定义与任务状态 |

### 操作规范（已固化到 `herdr/kernel.py` 与 `console/herdr_factory_console.py`）

1. **确定性控制元语层**：
   - 将受控逻辑独立封装在 `herdr/kernel.py` 中，支持 CLI（`herdr-factory step/rollback/force-pass/checkpoint`）与 REST API（`POST /api/kernel/*`）对等调用。
   - `step_workflow`：仅当工作流显式处于 `paused` 时才执行就绪节点挑选与单步推进，步进后严格维持暂停状态。
   - `rollback_workflow`：基于 `collect_downstream_nodes` 严格计算级联闭包，确保历史节点重放无死锁。
2. **零第三方依赖与原子持久化**：
   - 坚持纯 Python 标准库（`json`、`pathlib`、`time`、`uuid`），快照采用 `tmp + replace` 原子落盘，防止进程崩溃导致快照文件损坏。
3. **控制台可视化介入底座**：
   - 在控制台更多操作菜单与任务列表精准嵌入交互入口，对于回溯等破坏性操作一律配设二次确认与警示色提示。

### 验证命令 / 证据

```bash
# 1. 运行内核控制元语全套测试
/opt/homebrew/bin/pytest tests/test_kernel_control_primitives.py tests/test_console_kernel_api.py

# 2. 全量回归测试验证老逻辑零破坏
/opt/homebrew/bin/pytest

# 3. CLI 快速验证
bin/herdr-factory checkpoint --help
bin/herdr-factory step --help
bin/herdr-factory rollback --help
```

---

## 23. 工位实时干预网格（Steering Mesh）：即时软中断、插话队列与结构化干预协议

### 问题背景

在长耗时任务执行或自主多轮推理场景中，工位 Agent（如 Codex、Claude）容易因为理解偏差、提示词歧义或探索方向错误而陷入死循环或产出跑偏代码。早期系统缺乏对运行中 Agent 的有效干预手段：
1. **无法插话**：人类必须等待 Agent 彻底跑完当前全部 Turn 甚至耗尽 Token 后才能打回重做；
2. **缺乏紧急制动**：若要停止跑偏的 Agent，只能直接关闭终端 Pane，导致现场未提交代码与上下文彻底损毁；
3. **指令容易被淹没**：随意的终端输入无法被大模型明确解析为高优先级的总指挥干预指示。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 缺乏即时打断机制 | 暴力 kill 进程或关闭 Pane 会彻底损毁代码现场与 Git 索引 | 统一采用发送受控软中断（SIGINT / ctrl-c），使 Agent 安全停止当前输出，状态转为 `interrupted` 保留完整现场 |
| 无法在运行间隙平滑插话 | 强行打断容易破坏正在编译或测试的临界区代码 | 建立任务维度的有序插话队列（Steering Queue），支持「紧急立即中断插话」与「间歇自动排队注入」双通道 |
| 人类插话语义模糊 | 普通文本输入容易被 Agent 混淆为普通上下文或代码输入 | 制定结构化注入协议（`【总指挥实时插话纠偏指令 - STEERING INSTRUCTION】`），注入发起人与审计时间戳 |
| 巡检进程缺乏队列消费闭环 | 只有 Web/CLI 压入队列，无法自动感知 Agent 闲暇 | 联动 `herdr-sentinel`，在 Agent idle 探测周期中主动发现并消费未派发插话 |

### 操作规范（已固化到 `herdr/steering.py`、`bin/herdr-task` 与 `console/herdr_factory_console.py`）

1. **核心控制元语与状态机流转**：
   - 状态机扩展：在 `TRANSITIONS` 中引入 `interrupted` 状态，允许从 `dispatched`、`working`、`rework`、`blocked`、`paused` 流转至 `interrupted`，并可安全恢复为 `working` 或 `rework`。
   - `queue_steer`：支持持久化存储到 `~/.herdr-controller/steering.json`；若 `urgent=True`，触发立即软中断并派发。
   - `halt_task`：安全注入 `ctrl-c`，更新任务状态与审计历史。
2. **CLI 与控制台双向联动**：
   - CLI：`herdr-task halt <task_id>` 与 `herdr-task steer <task_id> "<instruction>" [--urgent]`。
   - 控制台：在每个活跃工位任务卡片上挂载「插话」与「制动」按钮，配设规范中文引导与模态确认框。

### 验证命令 / 证据

```bash
# 1. 运行干预网格与控制台接口测试
pytest tests/test_steering_mesh.py tests/test_console_steering_api.py -v

# 2. 全仓回归测试确保无回归
pytest

# 3. CLI 命令验证
bin/herdr-task halt --help
bin/herdr-task steer --help
bin/herdr-task steer-queue --help
```

---

## 24. 语义提炼引擎与白盒数据流（Projection Engine）：4D 白盒遥测、终端噪声清洗与产物第一公民投影

### 问题背景

在多智能体自主协同过程中，工位终端不断输出大量低级原始日志（如 ANSI 转义字符序列、VT100 光标控制码、编译器反复刷屏文本）。早期系统直接将原始终端流倾倒给控制台或 CLI，导致严重认知过载与黑盒感：
1. **终端噪声严重**：颜色码、反光标与进度条残留造成控制台和终端阅读体验极差；
2. **状态黑盒感强**：人类总指挥难以快速回答关键问题：“当前智能体究竟在尝试达成什么目标？”、“目前处于研发的哪个里程碑？”；
3. **交付产物被弱化**：Git 代码修改、测试评估报告（EVALUATION.md）散落在文件系统深处，缺乏结构化归集与高亮展示；
4. **卡点无法及时感知**：编译报错、缺少依赖或环境冲突被淹没在成百上千行终端日志中，未被结构化为醒目的卡点（Blocker）求助。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 原始终端流充满转义控制码 | 直接展示原始终端文本会导致严重的渲染混乱与视觉疲劳 | 引入纯标准库正则流式清洗 `strip_ansi_codes`，彻底滤除 CSI、OSC、光标指令与控制字符 |
| 缺乏高阶语义意图跟踪 | 单纯看最后一行终端命令（如 `Running test...`）无法代表宏观任务意图 | 制定语义提炼优先级：`[HERDR_INTENT]` 显式探针 > `task.goal` 顶层意图 > 瞬态执行动作 > 节点基础语义 |
| 交付产物未被结构化归集 | 人类必须进入子仓库手动 `git status` 或寻找评估文件 | 确立产物第一公民（Artifacts as First-Class Citizens）：动态投影 Git 差异摘要、内循环评分报告与设计文档 |
| 卡点求助被日志淹没 | 智能体受阻时缺乏直观警示 | 模式匹配编译错误、断言失败与依赖缺失，并在白盒卡片顶部以醒目警告条透出 |

### 操作规范（已固化到 `herdr/projection.py`、`bin/herdr-task` 与 `console/herdr_factory_console.py`）

1. **核心提炼引擎 (`herdr/projection.py`)**：
   - 4D 投影模型：意图 (Intent)、动态路标 (Milestones)、核心产物 (Artifacts)、卡点求助 (Blockers) 与近期动态 (Recent Activity)。
   - 纯标准库实现（遵循 Ponytail 原则，不引入第三方依赖）。
2. **CLI 投射子命令**：
   - `herdr-task project <task_id> [--json]`：展示结构化白盒任务简报。
   - `herdr-task artifacts <task_id> [--json]`：快速核验任务产生的所有第一公民交付物。
3. **控制台白盒卡片与 REST API**：
   - REST 接口：`GET /api/task/projection` 与 `GET /api/workflow/projection`。
   - 弹窗详情升级：任务详情模态框升级为白盒简报卡片（路标进度条、产物清单、当前意图与原始数据折叠切换）。

### 验证命令 / 证据

```bash
# 1. 运行投影引擎与控制台接口测试
pytest tests/test_projection_engine.py tests/test_console_projection_api.py -v

# 2. 验证前端模板语法契约
pytest tests/test_console_frontend_syntax.py tests/test_console_templates.py -v

# 3. 全仓 302 项自动化测试 100% 通过
pytest

# 4. CLI 命令交互验证
./bin/herdr-task project --help
./bin/herdr-task artifacts --help
```

---

## 25. 通用配置驱动与受控 MCP 生态容器：元模型解耦、沙盒权限隔离与跨领域无环拓扑校验

### 问题背景

早期系统的工作流强绑定在软件研发流程上（需求、计划、代码实现、测试、评审），节点属性与工具调用偏向硬编码，难以支持商业调研、合同会签、财务分析等跨学科复杂协同场景：
1. **输入与门禁语义僵化**：节点缺少显式的多源输入引用契约（`inputs`），质量门禁（`gate`）无法灵活表达自动准则与人工审批混合（`hybrid`）模式，且缺乏门禁失败时的定向回退目标（`retry_target`）；
2. **工具挂载缺乏权限沙盒**：工位智能体可以直接调用未受限的本地工具，存在在只读分析阶段意外执行写磁盘或外部提交的高危越权风险；
3. **拓扑校验不完全**：原有的 DAG 拓扑环路检测仅覆盖 `depends_on`，一旦节点在 `retry_target` 或 `inputs` 引用中隐式引入死循环或未知节点，会导致调度器静默崩溃。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 业务流程强依赖硬编码节点类型 | 真正的通用底座必须领域无关 (Domain-Agnostic) | 扩充通用节点元模型：支持 `inputs` 多源引用、`worker_policy` 能力与权限模型、以及 `gate` 混合门禁与 `retry_target` 回退配置 |
| 工具越权执行高危操作 | 仅靠提示词软约束无法确保安全合规 | 建立受控 MCP 工具池（`herdr/mcp.py`），依据节点声明的能力（`capabilities`）与权限级别（`read_only` / `read_write` / `require_approval`）做物理挂载与边界拦截 |
| 扩展属性可能引入断链或死锁 | 拓扑算法不能只看线性依赖 | 增强 Kahn 拓扑校验器 `validate_workflow_dag`，严密校验 `retry_target` 存在性与跨节点输入引用合法性 |
| 模式升级导致旧模板失效 | 不向后兼容是大型系统升级的灾难 | 保持 100% 向后兼容：旧模板缺省 `inputs`/`gate` 时自动优雅降级补齐默认值，现有全部模板零改动平滑运行 |

### 操作规范（已固化到 `herdr/workflow.py`、`herdr/mcp.py` 与 `workflow_templates/business-research-v1.yaml`）

1. **通用契约与拓扑防御**：
   - 节点规格：统一支持 `inputs: [{ref: "..."}]`、`worker_policy: {capabilities: [...], permissions: [...]}`、`gate: {type: "auto"|"human"|"hybrid", auto_criteria: "...", requires_human_approval: bool, retry_target: "..."}`。
   - 拓扑门禁：`validate_workflow_dag` 强制核验 `retry_target` 与 `inputs` 引用节点在拓扑中的合法存在性。
2. **受控 MCP 工具池治理**：
   - 内置高可用工具包：`web_search`、`data_extraction`、`file_system`、`git_tools`、`human_signoff`。
   - 挂载决策：`resolve_node_mcp(node)` 仅挂载能力交集且权限不超标的安全工具，通过 `check_node_permissions` 进行运行时拦截。
3. **跨领域通用模板示范**：
   - 落地 `business-research-v1.yaml`（市场界定 ➔ 情报抓取 ➔ 商业洞察 ➔ 高管简报会签），验证纯配置驱动在跨学科协同中的有效性。

### 验证命令 / 证据

```bash
# 1. 运行阶段四新增测试（动态模式 + MCP 插件池）
pytest tests/test_dynamic_workflow_schema.py tests/test_mcp_capability_mesh.py -v

# 2. 验证前端控制台模板库与语法契约
pytest tests/test_console_frontend_syntax.py tests/test_console_templates.py -v

# 3. 全仓 314 项自动化回归测试 100% 通过
pytest
```

---

## 26. 人机对等协同工作舱：注意力过滤模型、沉浸式成果会签与折叠式物理抽屉

### 问题背景

在多智能体流水线并发推进时，人类交互界面通常面临两大极端缺陷：
1. **认知过载与信噪比过低**：直接向人类倾泻全量 Agent 终端日志，面对十几个并发工位，人类总指挥无法在 5 秒内获知“哪些在正常推进、哪些遇到卡点、哪些正在等待我拍板”；
2. **缺乏结构化成果审批底座**：Agent 输出产物后，人类只能在终端或文件浏览器中找文件，缺少第一公民化的交付物会签室；遇到门禁阻断时无法快捷提供批注并回退重跑，难以形成高质量的人机协作内循环；
3. **物理现场与日常视窗强耦合**：强行隐藏原生终端会阻碍深度排障，而将终端长期平铺又造成巨大的视觉污染与性能损耗。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 30 个 Agent 同时跑，人类无所适从 | 认知注意力是系统最稀缺的资源 | 构建 **注意力中心 (Attention Hub)**：通过态势条与过滤标签（全部 / 待我拍板 / 需关注 / 进行中），首屏噪音降低 90% |
| 产物深藏文件系统，门禁决策脱节 | 产物（Artifacts）必须作为第一公民 | 构建 **成果交付会签室 (Artifact Signoff Chamber)**：沉浸式呈现交付物（Markdown、CSV、Diff、评分），提供「一键通过」与「批注打回」闭环 |
| 批注打回缺乏上下文联动 | 打回不能仅仅变状态，必须指导后续重跑 | 会签打回时支持选择 `retry_target` 节点，原子回溯工作流拓扑，并将人类修改意见以高优先级插话形式注入工位 |
| 终端平铺与完全隐藏的两难 | 物理现场应“随叫随到，平时隐蔽” | 构建 **底层物理抽屉 (Deep Physical Drawer)**：底部常驻折叠栏，点击秒级展开查看 Live TTY、内核日志与遥测 JSON，排障完毕一键折叠 |

### 操作规范（已固化到 `console/herdr_factory_console.py` 与 `tests/test_console_signoff_api.py`）

1. **三轨协同工作舱布局**：
   - 第一轨（顶部）：协同态势条（Attention Banner）动态提炼全局智能体协同状态；
   - 第二轨（主区）：任务看板支持一键切换「全部 / 待我拍板 / 需关注 / 进行中」，并为每个任务提供「成果会签」、「简报」、「插话」等行动点；
   - 第三轨（底部）：折叠抽屉支持 Live TTY、Controller Log、Raw Telemetry 三视图无刷新切换。
2. **会签室原子操作 API**：
   - 暴露 `POST /api/task/signoff`：支持 `action='approve'`（触发 `force_pass_gate`）与 `action='reject'`（触发 `rollback_workflow` 与 `queue_steer`）；
   - 前端无阻塞原生弹窗完成批注意见输入与确认。
3. **语法与无障碍安全防护**：
   - 所有新增前端代码均通过 `node -c` 脚本语法严格编译断言与 WCAG AA ARIA 无障碍属性检测。

### 验证命令 / 证据

```bash
# 1. 运行阶段五新增测试（Signoff API + 前端语法与交互契约）
pytest tests/test_console_signoff_api.py tests/test_console_frontend_syntax.py -v

# 2. 全仓 317 项自动化回归测试 100% 通过
pytest

# 3. 前端部署与同步验证
./scripts/install-herdr-console.sh
```

---

## 27. 跨阶段全链路集成测试的持久化沙盒隔离陷阱

### 问题背景

在开展通用人机协同底座跨阶段全链路端到端演练（E2E Dogfooding）时，编写检查点快照（Checkpoint）测试用例 `test_e2e_checkpoint_lifecycle_and_restoration`，在多次运行后发现断言快照列表长度偶发失败（预期只有当前测试创建的 1 个快照，实际却查出 2 个或多个）。
排查发现：底座各阶段模块分别引入了各自独立的持久化环境变量（如 `WORKFLOWS_FILE`、`TASKS_FILE`、`STEERING_FILE`、`HERDR_MCP_REGISTRY` 与 `CHECKPOINTS_DIR`）。在编写跨阶段集成测试的 fixture 时，开发者往往只 mock 了常见的工作流与任务文件，遗漏了检查点持久化目录 `CHECKPOINTS_DIR`，导致快照文件隐式落到了宿主用户真实目录（`~/.herdr-controller/checkpoints`），引发不同测试轮次之间的脏数据污染。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 集成测试快照列表计数膨胀 | 底座不同模块引入了各自独立的持久化环境变量 | 集成测试 fixture 必须梳理全量持久化重定向清单，杜绝漏网环境变量 |
| 快照写穿到宿主目录 | 模块默认 fallback 路径指向用户真实目录 | 在测试沙盒中，所有带 fallback 机制的路径变量必须强制全量 mock 到 `tmp_path` |
| 跨用例状态隐式污染 | 本地测试通过但多用例连续执行偶发失败 | 测试套件内严禁产生宿主用户目录的副作用文件 |

### 操作规范（已固化到 `tests/test_universal_substrate_e2e.py` 与 `scripts/verify-universal-runtime-e2e.py`）

1. **底座全景持久化变量重定向规范**：
   凡涉及工作流全链路集成测试的环境，必须全量重定向以下 5 个关键持久化路径：
   - `WORKFLOWS_FILE`: 工作流实体 JSON；
   - `TASKS_FILE`: 工位任务实体 JSON；
   - `STEERING_FILE`: 插话与干预队列 JSON；
   - `HERDR_MCP_REGISTRY`: MCP 插件注册表 JSON；
   - `CHECKPOINTS_DIR`: 检查点快照专用目录。
2. **测试前后环境自清洁**：
   - fixture 统一基于 `tmp_path` 构建独立目录树，测试结束后自动随临时目录销毁，彻底消除跨进程与跨测试的潜在污染。

### 验证命令 / 证据

```bash
# 1. 运行端到端全链路集成测试
pytest -v tests/test_universal_substrate_e2e.py

# 2. 运行独立端到端演练脚本
python3 scripts/verify-universal-runtime-e2e.py

# 3. 全仓全量回归测试 (321 项测试用例 100% 通过)
pytest
```

---

## 28. 嵌入式 SQLite 状态引擎：连接复用规避嵌套事务死锁、双写 ID 归一与图谱谱系分叉设计

### 问题背景

在推进北极星架构体系状态引擎升级（从分散的 JSON 文件迈向嵌入式 SQLite 存储，支持单事务原子快照、谱系溯源与时间旅行分叉）过程中，暴露出以下几类关键并发与数据一致性陷阱：
1. **嵌套操作连接隔离导致死锁**：在 `restore_checkpoint` 与 `fork_workflow_from_checkpoint` 等高级元语中，外层使用 `conn.execute("BEGIN TRANSACTION;")` 开启了独占事务；其内部若调用常规持久化方法（如 `save_workflow`、`save_task`），由于没有传递外层连接，子方法隐式开启新的 SQLite 连接并尝试写表，触发 `sqlite3.OperationalError: database is locked` 死锁异常；
2. **双写架构下的 ID 漂移裂脑**：在由 JSON 文件向 SQLite 平滑演进的过渡期（双写阶段），`kernel.py:create_checkpoint` 先自主生成了一个基于 UUID 的快照 ID 并写入 JSON 文件，随后调用 `state_db.create_checkpoint`；若 `state_db` 也默认内部独立生成新 UUID，会导致同一个业务检查点在 JSON 系统和 SQLite 系统中持有互不相同的 ID，造成按 ID 检索、还原与分叉时的全链路断裂；
3. **分叉衍生工作流的拓扑与锁状态污染**：从中间检查点进行时间旅行分叉（Fork）创建新实验分支时，若简单全量复制原工作流与任务状态，会连同原工作流的阶段推进锁（`stage_locks`）、调度完成标记和历史物理工位绑定一同拷贝，导致新派生的工作流在 Controller 调度器中处于锁死或幽灵状态。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| SQLite 事务内嵌套调用造成锁库 | 单一线程在未提交的事务连接外开启新连接写同一 SQLite 库必然死锁 | 核心持久化函数必须统一暴露可选 `conn: Optional[sqlite3.Connection] = None` 参数；外层事务必须显式下传 active 连接，内层若接收到外部连接则严禁执行 close() 或自动 commit() |
| 存储双写导致快照 ID 裂脑 | 跨介质持久化必须由单一源头确定第一公民业务实体 ID | `state_db.create_checkpoint` 必须支持接收外部指定的 `checkpoint_id`；由上层统一生成 ID 并同时注入双写层，保持介质间 1:1 精确对齐 |
| 时间旅行分叉残留旧环境锁 | 分叉是派生全新执行分支，不能无脑深拷贝物理运行时状态 | 分叉算法必须深度重置衍生实体的状态机：清除 `stage_locks`、清空残留任务状态并重置为初始待派发态，赋予全新的派生工作流 ID 与父级谱系指针（`parent_checkpoint_id`） |
| 无第三方依赖约束下的 WAL 并发 | 多进程/多线程读写容易出现 database locked 瞬态抖动 | 统一开启 SQLite WAL 模式（`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`），在零外部依赖下获得工业级读写分离并发能力 |

### 操作规范（已固化到 `herdr/state_db.py`、`herdr/kernel.py` 与 `tests/test_state_db_v2.py`）

1. **事务连接透传契约**：
   - 所有基础写操作函数：`save_workflow(wf, conn=None)`、`save_task(task, conn=None)`、`record_event(event, conn=None)` 必须支持外部连接透传；
   - 外部复合事务（如 restore、fork、migrate）统一使用 `with get_db() as conn: with conn: ...` 或显式 BEGIN/COMMIT 并将 `conn` 级联透传。
2. **双写 ID 归一与向后兼容**：
   - `create_checkpoint(workflow_id, label, checkpoint_id=None)`：允许上层指定统一 ID；
   - `kernel.py` 统筹生成单点 ID，确保 `checkpoints/<wf_id>/<cp_id>.json` 与 SQLite `checkpoints` 表主键完全一致；
   - 提供 `migrate_v1_to_v2()` 幂等双向平滑迁移工具。
3. **时间旅行与谱系追踪**：
   - 检查点结构包含 `parent_id`、`dag_snapshot`、`state_vector`；
   - `fork_workflow_from_checkpoint(checkpoint_id, new_workflow_id, ...)` 原子落盘新工作流与任务，精准清除阶段调度锁，保留 DAG 拓扑并记录衍生关系。

### 验证命令 / 证据

```bash
# 1. 运行 Checkpoint Store V2 状态引擎与内核桥接单元测试
pytest -v tests/test_state_db_v2.py

# 2. 运行全仓自动化回归（330 个测试用例 100% 全部通过）
pytest

# 3. CLI 命令验证
bin/herdr-task checkpoint-create --help
bin/herdr-task checkpoint-list --help
bin/herdr-task checkpoint-restore --help
bin/herdr-task checkpoint-fork --help
```

---

## 29. 研发任务启动现场隔离与分支纪律：远端拉取同步与 CoW 沙盒建支必须作为一等公民门禁，杜绝在脏主干或他人分支上直接提交代码

### 问题背景

在本次研发规范（纯核心与装配解耦、模块内聚反过度抽象、文件健康度梯度拆分）的落地过程中，由于缺乏严格的任务启动前置隔离检查，暴露出以下严重的工程协同与 Git 分支失范事故：
1. **未核验当前分支归属盲目提交**：本地工作区停留在前序任务的功能分支（`feat/checkpoint-store-v2-sqlite`，对应包含 1600+ 行 SQLite 状态引擎代码的 PR #17）上。在接收到“提交 PR”指令时，Agent 未做分支核对与远端检查，直接在当前他人分支上进行修改、提交并推送，导致纯规范文档修改被严重污染进他人的功能分支，险些引发代码审查与发布治理灾难；
2. **缺乏远端主干同步引发并发 PR 冲突**：当随后分别发起 PR #18 与 PR #19 时，因两个独立分支基于不同时序的旧基线修改了重叠的规范文件（`RULES.md`、`CLAUDE.md`、`wiki/log.md`），在其中一个 PR 合并后，另一个 PR 立即产生严重合并冲突；
3. **主干裸写与分支复用恶习**：长期依赖单一本地目录开发，容易把不同任务的修改、未跟踪文件和临时测试脚本相互混杂，违反了 Herdr 系统一贯坚持的空间隔离与沙盒理念。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 在未核验当前分支归属时直接开工 | 遗留分支或共享主干可能包含未合并或他人的工作，混淆提交会导致 PR 污染 | 任何任务启动前必须将远端拉取与沙盒建支作为**一等公民（First-Class Citizen）**前置执行 |
| 基于本地过期基线开工 | 本地主干若落后远端，后续提 PR 极易发生合并冲突 | 任务开工前强制执行 `git fetch origin` 同步本地 `main` |
| 在同一物理工作区直接切换分支/编码 | 容易残留未跟踪文件或混淆多个任务的上下文 | 必须利用 CoW (Copy-on-Write) 沙盒隔离机制（`herdr-task launch` 或独立克隆/Worktree）在新分支上闭环 |
| 并发 PR 冲突（如 PR #18 与 PR #19） | 两个并发分支修改同一规范文件必然冲突 | 遵循 `/unified-dev-flow` 统一研发流程，发现冲突按 `resolving-merge-conflicts` 规范解决并跑全测 |

### 操作规范（已固化到 `RULES.md §1.0 & §2`, `CLAUDE.md §3`）

1. **S0 准备阶段前置门禁（一等公民准则）**：
   - 任何任务启动前，必须强制执行：
     ```bash
     git fetch origin && git checkout main && git pull origin main
     ```
   - 必须使用 CoW (Copy-on-Write) 沙盒隔离机制（`herdr-task launch` 或独立沙盒目录）建立全新分支（如 `docs/<name>` 或 `feat/<name>`），严禁在主干工作区直接开发，严禁复用他人分支。
2. **任务收尾与 PR 提交前核验**：
   - 运行 `git diff origin/main...HEAD --stat`，确认变更文件 100% 仅包含当前任务相关的修改，无外部污染；
   - 运行 `pytest` 自动化测试套件确保 100% 绿灯通过；
   - 执行收尾知识沉淀，四段式追加至本文件，并同步更新 `wiki/log.md`。

### 验证命令 / 证据

```bash
# 1. 历史 PR 事故现场与修复证据
# PR #17: feat/checkpoint-store-v2-sqlite (误污染现场)
# PR #18: docs/remote-sync-and-cow-sandbox-rule (首个独立规范 PR & 冲突修复)
# PR #19: docs/upgrade-to-unified-dev-flow (全面升级统一流程规范 PR)

# 2. 全仓自动化回归测试（330 个测试用例 100% 全部通过）
pytest

# 3. 本地工作区纯净度检查
git status
```
---

## 30. 状态源统一与防裂脑：建立 StateStore 单一事实源，规避双状态源数据漂移与直接文件 I/O 陷阱

### 问题背景

在推进系统向 SQLite 嵌入式状态引擎演进的过渡阶段，系统存在严重的多状态源（Dual State Source）隐患：
1. **多模块直接操作原始 JSON**：`kernel.py` 与 `steering.py` 等关键业务模块内部直接执行 `open("tasks.json")`、`open("workflows.json")` 和 `open("steering.json")`，导致持久化事实源分散；
2. **双状态源数据裂脑风险**：调度内核控制原语（pause/resume/force_pass/rollback/step）和工位纠偏（steering queue/dispatch/halt）存在“SQLite 认为 A，JSON 认为 B”的时序漂移风险；
3. **事务一致性缺失**：直接文件操作绕过了数据库事务与 ACID 保障，在并发读写时易引发局部覆盖或读取到脏数据。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 模块各自直接读写 JSON 导致双状态源 | 任何直接 `open("tasks.json")` 的绕过行为都会导致状态真假难辨与裂脑 | 严禁任何业务模块自行打开 JSON 文件作为主状态存储；所有状态写入统一收敛至 `StateStore -> SQLite`，SQLite 为唯一运行时事实源 |
| 存量旧接口与兼容性断裂 | 骤然删除 `load_tasks_data` / `load_workflows_data` 会导致大量测试与外部工具崩溃 | 保留原有函数名作为只读兼容适配器（Adapter），内部代理到 `StateStore.export_*_json()`，且写入时统一写入 StateStore 并联动同步兼容文件 |
| 状态迁移与导出边界不清 | JSON 的角色定位必须从“主存储”彻底转变为“迁移/导出/兼容”载体 | StateStore 明确抽象出 `import_from_json()` 与 `export_*_json()` 协议；外部冷迁移或快照排障使用导出流，运行时严禁将 JSON 作为主状态载体 |
| 运行时反向同步引发状态倒退裂脑 | 若从磁盘读取 JSON 并覆盖 SQLite（如 `existing.status != json.status`），会导致旧 JSON 冲垮 SQLite 权威状态 | 严禁反向更新；从磁盘仅限冷导入 SQLite 中**完全不存在**的缺失实体（`if not store.get_task(tid)`），已有记录 100% 以 SQLite 为绝对事实 |

### 操作规范（已固化到 `herdr/state_store.py`、`herdr/state_db.py`、`herdr/kernel.py`、`herdr/steering.py`、`services/herdr-controller.py`、`bin/herdr-task` 与 `tests/test_state_store.py`）

1. **统一抽象层与工厂**：
   - 确立抽象接口 `StateStore(ABC)` 及标准实现 `SQLiteStateStore(StateStore)`；
   - 提供 `get_state_store()` / `set_state_store()` 单例与依赖注入入口；
2. **核心业务与调度器全量收敛**：
   - `kernel.py`、`steering.py`、`services/herdr-controller.py` 与 `bin/herdr-task` 的任务与工作流读写全面通过 `get_state_store()` 进行持久化与直接检索；
   - `auto_migrate_json` 默认设为 `False`，避免测试环境与静默调用时非预期导入本地残余 JSON 污染状态；
3. **单向派生与严格防裂脑**：
   - 严禁双向覆盖；JSON 仅作为 SQLite 的只读投射（Projection）或冷导出（Export），仅在冷启动遇到 SQLite 缺失实体时进行单向增量导入；
   - 伴生数据库路径基于任务文件推导（`p.with_suffix(".db")`），保证测试隔离性。

### 验证命令 / 证据

```bash
# 1. 运行状态引擎与单事实源防裂脑测试（验证 JSON 篡改无法污染 SQLite，CLI 直写实时生效）
pytest -v tests/test_state_store.py tests/test_state_db_v2.py

# 2. 运行内核控制与纠偏回归
pytest -v tests/test_kernel_control_primitives.py tests/test_steering_mesh.py

# 3. 全仓自动化回归（346 项测试 100% 全部通过）
pytest -q
```

---

## 31. 工作流抗停滞自愈、CoW沙盒纯净隔离与总指挥主动干预：终结长推理模型瞬态空闲死锁与母体代码污染

### 问题背景

在多 Agent 复杂编排与长时间运行的工作流（如商业研报生成 `wf-project-0913-01`）实战中，暴露出三处致命的工程死锁与推进阻塞缺陷：
1. **母体工作区脏修改（WIP）污染 CoW 沙盒**：`herdr-worker.py` 执行 `cp -cR source clone` 时完整拷入了未提交的脏代码。随后在沙盒内检出基于 `origin/main` 的分支时，Git 因未提交文件冲突而拒绝检出并崩溃；且半残 Clone 目录残留导致后续重试因已存在目录死锁；
2. **长推理模型瞬态停顿引发 Rework 孤儿死锁**：大模型（Codex/Claude）在深度推理或等待工具执行时存在瞬态停顿（>2s）。Controller 过早判定 `agent_done`，协调器因产物未落盘将其置为 `rework`。而在随后的状态机事件循环中，Controller 的 `idle` 事件仅响应 `working` 任务，对 `rework` 的完成事件完全丢弃；导致 Agent 产物最终落盘后任务被永久孤立在 `rework` 状态；
3. **总指挥侧缺乏全维停滞感知与干预手段**：当工作流因底层死锁或阶段推进悬挂停摆时，控制台无明确告警，缺乏“一键唤醒复审”或“重试推进”的白盒干预机制，人类总指挥无法在不断掉整个现场的情况下主动恢复。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| CoW 克隆拷入母体脏文件引发检出冲突 | 沙盒是独立副本，绝不能被宿主工作区的未提交改动阻碍分支切换 | 克隆建支前沙盒内部强制执行 `git reset --hard HEAD` 与 `git clean -fd` 纯净归一化；对非活跃残留 Clone 允许自愈覆写 |
| 仅凭终端空闲判定任务完成 | 瞬态网络卡顿或深度思考容易被误判为执行结束，引发下游协调器抢跑误判 | 引入产物契约优先原则（`check_task_deliverables_ready`），严格校验 `required_outputs` 或 baseline 真实变化 |
| Rework 状态完成事件被静默丢弃 | 状态机未闭环覆盖返工状态下的完成信号，导致返工任务沦为永久僵尸 | 事件循环、重启对齐与后台巡检全量接入产物就绪检测（`rework_watchdog`），自动推进 `agent_done` 并促醒协调器复验 |
| 调度停顿不可见与不可救 | 自动化系统难免偶发边界卡顿，缺乏白盒干预手段会导致用户只能粗暴重来 | 建立全维停滞感知（`detect_workflow_stalls`）、Attention Banner 警告横幅与人工干预通道（Force Review / Retry Advance） |

### 操作规范

1. **沙盒纯净隔离与异常自愈**：固化到 `services/herdr-worker.py` 的 `sanitize_clone_sandbox` 与 `create_clone`；
2. **产物契约校验与 Rework 看门狗**：固化到 `services/herdr-controller.py` 的 `check_task_deliverables_ready`、`handle_event`、`reconcile_task_state` 与主循环 `rework_watchdog`；
3. **总指挥停滞感知与主动干预控制台**：固化到 `herdr/projection.py`（`detect_workflow_stalls`）、`console/herdr_factory_console.py`（`/api/task/force-review`、`/api/workflow/retry-advance`、Attention Banner 告警条与任务卡片唤醒按钮）。

### 验证命令 / 证据

```bash
# 1. 运行沙盒隔离与返工自愈回归测试
pytest -v tests/test_herdr_worker.py tests/test_fix_loop_anti_flapping.py

# 2. 运行白盒遥测停滞检测测试
pytest -v tests/test_projection_engine.py

# 3. 全仓自动化回归（349 项测试 100% 全部通过）
pytest -q
```

---

## 32. 核心控制读取 Fail-Closed 铁律：彻底关闭关键控制链路的 Read Fallback，杜绝过时 JSON 导致的错误路由与幽灵推进

### 问题背景

在实现“写入型双状态源 Fail-Closed”后，系统在正常路径下已完全以 SQLite (`StateStore`) 为权威。但在边缘故障与异常处理场景中，部分关键控制读取链路（Router 路由决策、Controller 活跃工作流推进扫描、Projects 工作流注册与终态查重）仍残留了静默吞掉数据库异常后回退到磁盘 `workflows.json` 或 `tasks.json` 的 `Read Fail-Open` 代码逻辑：
1. **过时镜像诱发错误决策**：若 SQLite 发生瞬间并发锁等待或 I/O 故障，而磁盘 JSON 恰好落后一拍（例如 JSON 记录的任务仍为旧状态或旧代理），Router 会基于过时 JSON 做出错误的分发与负载统计；
2. **终态状态逆转导致幽灵推进**：`projects.non_terminal_workflow_ids()` 与 `active_workflows_for_project()` 若在读取异常时回退到旧 JSON，已在 SQLite 中标记为 `completed` 的工作流可能在 JSON 中仍显示为 `running`，导致 Controller 重新唤醒已结案工作流并诱发幽灵推进事故；
3. **假阳性与故障掩盖**：吞掉 SQLite 读取异常使得真实的数据库连接泄漏、文件锁超时或表损坏无法在监控中暴露，阻碍可观测性建设。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 读取故障静默降级到陈旧 JSON | 核心控制链路（Routing/Advance/Registration/State Transition）绝不能依据非权威或陈旧的数据做决策 | 核心控制读取必须遵守 Fail-Closed 铁律：底层 StateStore 报错直接向上阻断，严禁静默降级到 JSON |
| 读链路自动冷导入导致状态“起死回生” | 每次查询扫描 `workflows.json` 或 `tasks.json` 并插回 SQLite 会让已被删除/已归档或外部篡改的记录复活 | 彻底废除常规读链路上的 `_sync_missing_workflows_into_store`、`_sync_missing_tasks_into_store` 与 `_import_missing_tasks_from_disk`；仅在空库初建（`schema_meta` 标记 `v1_migration_done`）执行严格原子的一体化导入，运行时严禁任何从 JSON 反向写入 SQLite 的行为 |
| Checkpoint 读与分支操作 Fail-Open 倒灌 | 检查点读取/分叉若保留旧磁盘扫描与 SQLite 写回，会导致已删除状态经由 `cp_*.json` 偷渡复活 | `kernel.list_checkpoints`、`get_checkpoint` 与 `fork_workflow_from_checkpoint` 100% 仅依赖 `StateStore`；磁盘遗留 checkpoint 仅在首次建库 Bootstrap 时一次性导入，运行期查无记录直接抛出 `FileNotFoundError` 阻断 |
| 未注册工作流静默降级到 opencode | 调度路由查不到工作流时静默 fallback 会绕过项目池黑名单、健康准入与 reservation 并发锁 | 当指定了 `workflow_id` 但在 StateStore 查无记录时，必须直接抛出 `RuntimeError` 拒绝调度，仅限无 workflow_id 的独立任务走默认代理 |
| projects.json 倒灌幽灵工作流 | 调度器从辅助项目注册表追加未完成 workflow 会导致已结案记录形成幽灵活跃流 | `active_registered_workflows()` 100% 仅源自 `store.list_workflows()`，彻底清理跨表倒灌逻辑 |
| 启动数据继承过程缺乏原子性与阻断力 | 启动时部分遗留 JSON 格式损坏若吞掉异常静默启动，会导致系统在空库上裸跑且数据永久丢失 | 初始化迁移必须由单次数据库事务（`BEGIN TRANSACTION;` ... `COMMIT;`）保护；任一历史文件损坏立即 `ROLLBACK;`、不标记 `v1_migration_done` 并显式 `raise` 阻断启动，保留外部修复后重试通道 |
| 旧 SQLite 升级无 marker 误触发 Bootstrap | 若仅判断无 migration marker 就导旧 JSON，升级前已有 SQLite 数据的系统会被落后的 JSON 镜像覆盖（例如 completed 被覆盖为 running） | 在 Bootstrap 前必须先探活核心表业务数据（`has_existing_state`）；若已有数据则说明 SQLite 本身已是权威事实源，直接补 marker 绝不读取旧 JSON；仅当库完全为空且有 legacy JSON 时才允许 Bootstrap |

### 操作规范（已固化到 `herdr/agent_router.py`、`herdr/projects.py`、`herdr/kernel.py`、`herdr/steering.py`、`bin/herdr-task`、`services/herdr-controller.py`、`herdr/state_db.py` 与 `tests/test_critical_reads_fail_closed.py`）

1. **路由与负载计算收口**：
   - `agent_router.workflow_record()` 废除 `_sync_missing_workflows_into_store`；
   - `agent_router.choose_agent()` 对传入但未注册的 `workflow_id` 显式抛出 `RuntimeError("Workflow not found in authoritative StateStore: ...")`；
   - `_clean_reservations()` 与 `_active_agent_loads()` 废除 `_sync_missing_tasks_into_store` 与 JSON 降级，StateStore 异常直接抛出阻断；
2. **任务与检查点运行时事实源纯化（彻底根除从 JSON 偷渡复活）**：
   - `herdr/kernel.py` 彻底移除 `_import_missing_tasks_from_disk`，`load_tasks_data()` 纯净输出 `store.export_tasks_json()`；
   - `herdr/kernel.py` 彻底移除 `list_checkpoints`、`get_checkpoint` 与 `fork_workflow_from_checkpoint` 中的磁盘扫描与写回 SQLite 逻辑，全部纯净委托 `StateStore`；
   - `herdr/steering.py` 彻底移除 `load_tasks_data()` 读取 `tasks.json` 的旁路；
   - `bin/herdr-task` 与 `services/herdr-controller.py` 的 `load_tasks()` 100% 仅返回 `store.list_tasks()`，移除从磁盘向 SQLite 冷插入的新任务逻辑；
3. **工作流生命周期与注册表收口**：
   - `projects.load_workflows()`、`active_workflows_for_project()`、`non_terminal_workflow_ids()`、`project_for_workflow()` 与 `generate_workflow_id()` 彻底废除 `_sync_missing_workflows_into_store`，严禁回退或读回写入 SQLite；
   - 调度看门狗 `herdr-controller.py` 的 `_workflow_entry()` 仅纯净查询 StateStore；`active_registered_workflows()` 100% 仅查询 `store.list_workflows()`，清理从 `projects.json` 注入 `wf` 的幽灵链路；
4. **启动 Bootstrap 原子事务、旧库升级防覆写与 Fail-Closed 阻断保障**：
   - 数据库初始化在 `_ensure_schema` 中检测到未置位 `v1_migration_done` 时，**首先核查核心表（`workflows`, `tasks`, `checkpoints`, `steering_items`, `events`）是否已有业务数据**；若已有数据（旧版 SQLite 升级场景），说明 SQLite 本身已是权威事实源，直接写入 `v1_migration_done = '1'`，严禁读取任何磁盘 legacy JSON 避免状态被陈旧镜像覆写；
   - 仅当 SQLite 完全为空且存在 legacy JSON 时，才通过显式事务包裹历史文件继承；任一文件损坏立即回滚、绝不置位 `v1_migration_done`、不缓存连接，并显式 `raise` 阻断系统在损坏状态下裸跑，待文件修复后可安全重试导入。

### 验证命令 / 证据

```bash
# 1. 运行核心控制读取 Fail-Closed 专项测试套件（17 项测试，含 Test A~H 全量对抗场景）
pytest -v tests/test_critical_reads_fail_closed.py

# 2. 运行单事实源防篡改与全流程 E2E
pytest -v tests/test_state_store.py tests/test_universal_substrate_e2e.py

# 3. 全仓自动化回归（368 项测试 100% 全部通过）
pytest -q
```

---

## 33. 工程语言收敛与反过度承诺准则：剔除危险绝对化承诺与不可控 SLA，坚持事实驱动与严谨务实的系统定位

### 问题背景

在项目的迭代演进中，主文档（如 `README.md`）及多处核心指引逐渐滋生出带有“过度包装”、“绝对化承诺”、“假想 SLA”以及“AI 宣传味”的工程表述：
1. **“100% 向下兼容”的绝对化承诺**：任何软硬件系统的演化与 Schema 升级，都无法在理论或实践上保证永久的“100%”。一旦未来底层结构或协议发生必要重构，这种承诺会立即被打破或给后续演化造成过度负担；
2. **“毫秒级自动修复/自愈”的制造假想 SLA**：系统的核心优势在于“在任务派发前进行确定性探活与自动检测自愈”，将不可测的耗时指标（受 OS 进程调度、LaunchAgent、I/O 等影响）作为对外宣传，既不必要也制造了无谓的 SLA 争议；
3. **“生产级通用操作系统底座”的过度宏大定位**：脱离了 Herdr 当前作为实用的多 Agent 空间协同与工位调度底座的实际范围；
4. **“无副作用沙盒”、“阻断死锁”等重度承诺**：探针运行包含进程调用、网络与凭证检查，无法做到数学意义上的“无副作用”；其作用是识别 Agent 可用性并避免向故障 Agent 派发，而非解决全局死锁；
5. **“严格遵循最佳实践”等虚浮 AI 腔调**与机制描述失真（如将内存 dict 归一化写成“写回配置文件”）。

### 经验教训

| 现象 / 浮夸表述 | 潜在工程隐患与反思 | 规范替代与收敛策略 |
|---|---|---|
| “100% 向下兼容” | 绝对化断言，无视未来架构演进与配置 Schema 破坏性变更的可能性 | 收敛为“兼容现有 legacy stage-based workflow”，明确具体支持范畴与平滑过渡目标 |
| “毫秒级自动修复 / 自愈” | 虚构不可控的硬性执行 SLA，容易在物理环境受限时失信 | 收敛为“任务派发前自动检测并修复”，聚焦机制事实（Pre-dispatch Detection & Repair）而非承诺时间 |
| “生产级通用操作系统底座” | 概念过度包装，与实用的多 Agent 终端工位调度底座产生错位 | 收敛为“实用的多 Agent 工作流协同底座”，客观反映产品功能与工程边界 |
| “任何业务流程……均通过定义” | “任何”属无边界全称命题，实际上并非所有流程都已实现模板支持 | 改为“支持通过声明式 YAML/JSON 模板定义研发、标书、客诉等多类业务流程” |
| “无副作用沙盒实测” | 探针涉及进程调用、网络与环境读取，不可能在数学上绝对“零副作用” | 改为“隔离的沙盒实测验证”，强调隔离环境而非虚妄的零副作用 |
| “阻断死锁与无效分发” | 容易被曲解为探针能消灭分布式或全局所有死锁 | 改为“降低无效分发和因 Agent 不可用造成的阻塞风险”，严格契合 Agent 准入机制 |
| “严格遵循现代分布式……最佳实践” | 虚浮的 AI 赞美套话，缺乏明确客观标准 | 改为直接事实陈述：“仓库按核心库、CLI、后台服务、控制台、测试与文档分层组织：” |
| 机制描述失真（脑补磁盘写入） | 将内存中的 `normalize_workflow` 描述为“在配置文件中自动双向映射” | 严格尊重代码事实：“在加载时统一生成 `nodes` 与 `stages` 的兼容表示” |

### 操作规范（已固化到 `README.md`、`CLAUDE.md`、`docs/guides/` 与 `wiki/`）

1. **绝对化用语一律清除**：主干 README 与官方文档严禁出现“100% 兼容”、“绝对无死角”、“阻断所有死锁”、“任何流程”等全称不可控断言；
2. **拒绝制造外部 SLA 承诺**：内部调度机制描述应强调“触发时机”与“动作确定性”（例如“任务派发前自动检测并修复”），严禁使用“毫秒级”、“瞬时”等易受环境干扰的时间承诺修饰；
3. **精准声明兼容与防护边界**：兼容性说明必须具体指向具体的存量实体（如 legacy stage-based workflow、旧版 `--stage` 别名）；健康探针明确为“隔离沙盒实测验证与准入防阻塞”；
4. **剔除无信息量 AI 套话**：目录结构、工程设计直接展示事实分层与代码组织，不使用“严格遵循业界最佳实践”等虚浮空话；
5. **文档描述必须严格与代码机制对齐**：撰写文档时严禁凭印象想象代码行为（如混淆内存变换与磁盘持久化），必须以函数签名与真实返回值（如 `normalize_workflow`）为唯一事实来源。

### 验证命令 / 证据

```bash
# 1. 检查主文档与全仓核心文档，确认危险承诺与绝对化关键词已收敛
git grep -iE "(100% 向下兼容|100% 兼容|毫秒级自动修复|毫秒级自动自愈|操作系统底座|无副作用的沙盒|阻断死锁)" README.md docs/ wiki/

# 2. 全仓自动化回归测试保持 100% 绿灯通过
pytest -q
```


---

## §34 原型实现勿冒充通用协议：以 TTY Steering 为例的 AgentAdapter 解耦

### 问题背景

`dispatch_steer_now()` 以 `ctrl-c → sleep(0.1) → send-text → enter` 的纯 TTY 按键模拟作为 "Universal Agent Steering" 流通。这个实现能工作，但将其标签为"通用跨 Agent Steering 协议"是个技术谎言：OpenCode（auto 模式工具循环）、Codex（多轮对话 Session）、Claude（context 保持打断恢复）、Qoder（私有 TUI）、Agy（特殊 stdin 行为）对 Ctrl-C 信号捕获、提示词注入、Session 状态保持和中断后恢复的行为截然不同。若未来不同 Agent 需要差异化处理，极可能在 `steering.py` 内部演化出大量 `if agent == "claude": ... elif agent == "codex": ...` 分支，严重违反关注点分离，将核心调度层变成知识污水池。

### 经验教训

| 陷阱 | 说明 |
|---|---|
| **原型冒充通用协议** | 能跑通 ≠ 通用。应在 commit 时即明确标注当前实现的协议等级（`tty_prototype`），不过度标签 |
| **Adapter 绑定 TTY 实现** | 基础 `AgentAdapter` 若直接耦合 `ctrl-c`/`send-text`/`pane_id`，未来 RPC/API Adapter 就会隐式继承 TTY 副作用。必须拆分为纯契约 `AgentAdapter` 与传输实现 `TTYAgentAdapter` |
| **未知 Agent 乐观假设** | 未知 Agent 绝不能默认假设支持 TTY 信号。未知必须 Fail-Closed（`UnknownAgentAdapter` 所有能力全 `False`，拒绝执行干预） |
| **能力声明沦为说明书** | `supports_soft_steer=False` 时若仍向 TTY 注入，能力矩阵就只是装饰。声明不支持时必须在 Adapter 层直接阻断并返回 `ok=False`，严禁偷偷 fallback |
| **物理发送失败掩盖为已分发** | TTY 物理投递失败若将指令标记为 `dispatched`，指令就会永久丢失。投递失败必须保持 `pending` 并记录 `last_delivery_error`；interrupt 失败必须阻止 Task 进入 `interrupted`，防事实漂移 |
| **半途失败反向事实漂移** | Urgent Steer 存在“Ctrl-C 成功但 prompt 注入失败”：Agent 真实已停止，若 Task 保持 working 则发生反向漂移。必须推进 Task 为 `interrupted` (`requires_attention=True`)，指令保持 pending 待重发 |
| **反向依赖与传输不纯粹** | Adapter 若反向调用 Steering 内部的私有按键方法，会导致循环依赖。Steering 编排层必须 100% 零 Subprocess；所有按键逻辑必须收敛于 `TTYAgentAdapter` |
| **快照保存引发历史重复膨胀** | Audit History 必须真正 append-only。严禁在状态保存函数中遍历全量历史重新 insert，否则重试越多历史膨胀越严重，直接污染下游 Event Stream |

### 操作规范

1. **抽象契约与传输解耦**：基础 `AgentAdapter` 零 TTY/Pane/Subprocess 知识；`steering.py` 零 Subprocess 导入；所有终端按键模拟严格下沉至 `TTYAgentAdapter`；
2. **Fail-Closed 默认安全**：`AgentCapability` 默认全部 `False`，未注册 Agent 降级为 `UnknownAgentAdapter`，拒绝一切 Steering；
3. **能力即门禁**：`supports_soft_steer=False`（如 OpenCode/Qoder）时必须拒绝软插话并阻止物理写入，提示使用带中断的 urgent steer；
4. **真实交付语义（Anti-Skew）**：
   - 物理投递失败或无 Pane 时，指令状态严格保持 `pending`，返回 `ok=False`、`pane_delivery_ok=False`；
   - 中断信号物理发送失败时，`halt_task` 严格拒绝将 Task 状态推进为 `interrupted`，保持原状态并记录 `task_halt_failed`；
   - Urgent Steer 半途失败（中断成功、注入失败）时，Task 强制推进为 `interrupted` (`requires_attention=True`)，指令保持 `pending`，杜绝真实进程已死但数据库仍在 working 的反向漂移；
5. **真正的 Append-Only 历史记录**：每次操作仅单向追加 1 条历史事件至 SQLite，`save_steering_data` 仅同步状态与队列，严禁重放全量历史；
6. **门禁与审计追溯**：每个干预指令在 StateStore 与任务历史中均附带 `protocol` 与 `adapter` 元信息，全量回归验证 386 项全绿。

### 验证命令 / 证据

```bash
# 1. AgentAdapter 契约纯净度、Fail-Closed 与 Soft-Steer 阻断门禁
pytest tests/test_agent_adapter.py -v

# 2. Steering Mesh 物理投递失败保持 pending、半途失败防漂移、历史不重复膨胀
pytest tests/test_steering_mesh.py -v

# 3. 全量回归（基线 368 → 386 passed）
pytest -q

# 4. CLI 能力矩阵查询验证
bin/herdr-task adapters
```

---

## 35. WorkflowEvent Contract 与首批生产者接入：先统一事件骨架，再逐步补齐 Task/Workflow/Node 生命周期事件

### 问题背景

在 StateStore 完成 SQLite 单一事实源后，运行时仍存在多条语义相近但形态分散的历史链路：`events` 表仅保存部分 checkpoint/fork 事件，`steering_history` 记录人工纠偏历史，task payload 内又保留 task history 片段，Projection/Dashboard 仍主要从任务表和终端缓冲拼装当前视图。本阶段只建立统一 `WorkflowEvent` 契约并接入 checkpoint/fork/steering 等首批生产者；Task/Workflow/Node 的完整生命周期事件化必须单独规划，不能在事件骨架 PR 中顺手做大。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 事件事实散落在业务表与兼容历史表中 | 状态表描述当前状态，事件流描述发生过什么；两者职责不能混用 | 本阶段新增的 checkpoint/fork/steering 事件必须写入 `WorkflowEvent`；业务历史表保留兼容读模型或局部索引 |
| 事件缺少 node/agent/source 维度 | Dashboard、Audit、Notifier、Replay 与 Metrics 的消费维度不同，缺字段会迫使消费者反查任务表或猜测来源 | `WorkflowEvent` 至少包含 `workflow_id`、`node_id`、`task_id`、`agent_id`、`event_type`、`timestamp`、`payload`、`source` |
| 内部事件源各自手写 `INSERT INTO events` | 分散写入会在扩字段、事务透传和索引策略变化时产生半新半旧记录 | 所有写入统一经过 `state_db.record_event()` 或 `StateStore.record_event()`，事务内路径通过 `conn` 透传保持原子性 |
| 为统一模型顺手改旧事件类型 | 事件名是消费者契约，重命名会破坏旧 Dashboard、审计脚本或测试夹具 | 扩字段不改语义名；`checkpoint_created`、`checkpoint_restored`、`workflow_forked` 等既有 `event_type` 必须保留 |
| 只新增 API 未验证旧路径进入 Stream | 新消费者会误以为首批生产者已完整接入，实际 checkpoint/fork/steering 等关键源可能仍漏写 | 测试必须覆盖新 API 与首批生产路径：直接 record/list、checkpoint create、workflow fork、steering history 都要能从统一 stream 读到 |
| 过早宣称 Canonical Stream 完整可替代所有读模型 | 当前 Task/Workflow/Node 状态转换尚未全部事件化，只读 Event Stream 会漏掉核心生命周期 | 本 PR 只声明 `WorkflowEvent Contract + first producers`；完整生命周期事件化与增量 cursor 放入后续 PR |

### 操作规范（已固化到 `herdr/state_db.py`、`herdr/state_store.py`、`tests/test_state_db_v2.py` 与 `tests/test_state_store.py`）

1. **事件表扩展只做兼容升级**：
   - `events` 表新增 `node_id`、`agent_id`、`source`；
   - `_ensure_event_columns()` 对既有库原地补列，保留旧列与旧事件类型。
2. **统一写入与查询入口**：
   - `state_db.record_event(event, conn=None)` 负责校验 `event_type`、要求 `payload` 为 dict、规范化 `node`/`agent` 别名并保留显式 timestamp；
   - `state_db.list_events(...)` 支持 workflow/node/task/agent/type/source/limit 过滤，按 `timestamp ASC, id ASC` 稳定回放；
   - `SQLiteStateStore.record_event()` 与 `SQLiteStateStore.list_events()` 作为上层唯一公开入口。
3. **现有事件源同步进入统一 Stream**：
   - `record_steering_history()` 继续写 `steering_history`，同时追加 `steering.<action>`；
   - `create_checkpoint()`、`restore_checkpoint()`、`fork_workflow_from_checkpoint()` 不再手写 SQL，统一经 `record_event()` 追加事件。

### 验证命令 / 证据

```bash
# 1. WorkflowEvent schema/API 与 checkpoint/fork/steering 旧路径回归
pytest tests/test_state_db_v2.py -q

# 2. StateStore 公开接口与兼容导出回归
pytest tests/test_state_store.py -q

# 3. 全仓自动化回归
pytest -q
```

---

## 36. Kernel State Transition Gateway：状态变迁与事件流的单一事务收敛，及单向投影与严格单一事实源原则

### 问题背景

在引入控制原语与 StateStore 统一事实源后，运行时仍有多处组件（`herdr-task` CLI、`herdr-factory`、`services/herdr-sentinel.py`、`herdr/steering.py`）直接修改 `task["status"]` 或 `workflow["status"]`。这种分散的状态突变导致：
1. 状态跃迁不受约束，非法跃迁（如从 `pending` 跳跃至 `completed`）无法被统一拦截；
2. 状态变迁与 `WorkflowEvent` 事件流脱节，事件审计流遗漏了最核心的生命周期事件；
3. 过渡期曾试图在运行时保留 JSON 反向倒灌 SQLite 以适配未初始化 DB 的旧测试，破坏了 SQLite 作为唯一事实源（Single Source of Truth）的原则，引发幽灵任务复活与跨进程写覆盖竞态；
4. 资源清理中曾出现“先拆解实体（finalize task/close tab/purge clone），最后才做状态转移校验”的顺序倒置，导致非法转移抛错时物理现场已被破坏；
5. 快照全量写回（Snapshot UPSERT）反向击穿 Gateway：当操作仅需更新元数据（如 `stage_verdict`、`paused_nodes`、`gate_overrides`、`history`、`commit` 等）时，若读取旧内存快照并调用全量 `save_tasks` / `save_workflows`，会倒灌陈旧的 `status` 字段，从而在没有 `WorkflowEvent` 的情况下静默覆盖并发 Gateway 刚刚推进的最新状态。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| **状态修改入口分散且缺乏约束** | 状态机逻辑若散落在各 CLI 与守护进程中，规则修改极易遗漏，非法跳转无法自证 | 建立 Functional Core（`herdr/transitions.py`）集中管理状态矩阵与纯校验逻辑，严禁各模块手写内联 transitions 字典 |
| **状态落库与事件追加脱节** | 状态写完了但事件写入失败，或者事件写入成功但状态未持久化，导致状态快照与事件重放流不一致 | 在 `state_db.py` 中将状态持久化与 `record_event(..., conn=conn)` 收敛在同一个 SQLite `BEGIN IMMEDIATE` 事务内，强保原子性 |
| **严格单一事实源与防止测试倒灌生产** | 曾试图在生产读取代码中保留 JSON 倒灌 SQLite 以适应未初始化 DB 的旧测试，导致幽灵任务复活与事实源裂脑 | 绝不因为测试 fixture 遗留而在生产代码中开 JSON 倒灌口；测试必须显式通过 StateStore 预置基准；Sentinel 与 Steering 仅能操作 DB 现有任务，缺失实体一律 skip 绝不 `save_task` 逆向注入；JSON 投影由 `sync_*_projection` 基于 `fcntl.flock` 跨进程锁单向覆写 |
| **门禁先于物理副作用（Gate before Side Effect）** | `close_workflow` 若先拆解任务、关闭 tab、清理 clone，最后调用 transition 才报错，会导致命令失败但现场已被破坏 | 任何物理 teardown 必须前置纯校验（`validate_workflow_transition(cur_status, "completed", force=force)`），前置门禁通过后才允许执行物理清理与状态提交 |
| **正常业务逻辑严禁滥用 `force=True`** | 在完成工作流等正常操作中曾盲目使用 `force=True` 绕过状态机，导致 `pending`/`paused` 等非运行态非法跳到 `completed` | 业务流转必须走合法路径（`running`/`in_progress` -> `completed`）；`force=True` 仅保留给管理员显式指定 `--force` 参数以逃生故障 |
| **异常静默吞没隐藏真实根因** | 在调用 Gateway 时若随意 `except Exception: pass` 吞掉合法性校验错误，会掩盖非法转移并继续执行旧的非法逻辑 | 非法转移（`InvalidTransitionError`）必须坚决抛出或在 CLI 明确打印并以约定退出码（exit 2）退出；严禁捕获异常后继续执行旧有旁路覆写 |
| **元数据更新禁止覆写状态（Metadata Isolation）** | 仅改 verdict/notes/history/locks 等元数据时若做全量实体覆写，旧快照会静默踩踏并发的新状态 | 建立原子元数据更新网关（`update_task_metadata()` / `update_workflow_metadata()`）：在 `BEGIN IMMEDIATE` 事务内重载实体、校验非保护字段白名单（`PROTECTED_*_FIELDS` 严防篡改 `status`/`task_id`）、应用变更并落库，彻底消除快照覆写隐患 |
| **Teardown 物理销毁 TOCTOU 竞态** | 若只做只读前置校验就启动不可逆资源销毁（关 Tab/删 Clone），销毁期间并发 pause 成功会导致终态提交失败，陷入资源已毁但状态停留在 paused 的撕裂 | 引入中间态 `closing`（`running`/`in_progress` -> `closing` -> `completed`）；在任何物理清理前先原子将状态推进为 `closing` 预占所有权；`closing` 状态下天然拒绝 `paused`，物理销毁完成后再流转至 `completed`，消除 TOCTOU 竞态 |
| **投影文件优先级混乱** | 若在同步 JSON 投影时优先取 `db_path.parent`，会覆盖调用方显式配置的 `TASKS_FILE` / `WORKFLOWS_FILE` 独立投影路径 | 统一收口解析优先级（`resolve_*_projection_file`）：`explicit argument -> os.environ -> store.db_path.parent -> default CONTROLLER_DIR` |
| **事件审计流元数据篡改防伪** | 若将调用方传入的 metadata 直接追加在事件 payload 和状态历史末尾，恶意或失误的元数据（如 `from_status` / `source`）会篡改真实审计字段 | 建立双重防伪机制：1. `RESERVED_EVENT_METADATA_FIELDS` 校验（违规直接抛 `ValueError`）；2. 结构级防御：写入 `status_history` 与 `event_payload` 时规范字段置于末尾覆写，确保核心审计事实不可伪造 |
| **后台守护服务启动环境依赖脆弱** | 守护进程脚本（如 `services/herdr-sentinel.py`）若直接独立执行，`sys.path[0]` 为 `services/`，未显式注入仓库根目录会导致 `from herdr.state_store...` 报 `ModuleNotFoundError` | 在文件最顶部显式注入 `HERDR_ROOT` 到 `sys.path[0]`，确保后台常驻看门狗在任何工作目录下均可开箱即用 |
| **同状态更新 verdict/note 伪装跃迁** | 任务已处于 `completed` 等状态时，若仅补录 `verdict`/`note` 仍调用 `transition_task`，会产生伪 `completed -> completed` 审计事件与冗余历史 | 同状态属性变更属于纯元数据操作，严格通过 `update_task_metadata()` 更新，绝不伪装为生命周期状态跃迁 |
| **自动补全父工作流产生非法 `unknown` 状态** | `save_task` 为防外键约束自动补全父工作流记录时曾赋予 `"unknown"` 状态，而状态机中并无此状态，导致产生 Gateway 无法流转的死锁工作流 | 自动补全的父工作流必须赋予状态机合法初始态 `"pending"`，确保后续可合法跃迁推进 |

### 操作规范

1. **函数式核心与命令式外壳解耦**：
   - `herdr/transitions.py` 纯逻辑：`TASK_TRANSITIONS`、`WORKFLOW_TRANSITIONS`、`ACTIVE_TASK_STATUSES`、`COMPLETED_TASK_STATUSES`、`TERMINAL_TASK_STATUSES`、`validate_task_transition()`、`validate_workflow_transition()`。零 I/O、零第三方依赖。引入 `closing` 状态（允许流转至 `completed` 或 `failed`）。
2. **唯一状态变更网关**：
   - `herdr.kernel.transition_task()` 与 `herdr.kernel.transition_workflow()` 作为全系统状态推进的唯一法定入口；
   - 统一由 `StateStore.transition_task()` 与 `StateStore.transition_workflow()` 在底层 SQLite 强事务内原子写入数据表与 `WorkflowEvent`（`event_type="task_transition"` / `"workflow_transition"`）。
3. **全量上游与控制原语改造**：
   - `kernel.pause_workflow()`、`kernel.resume_workflow()`、`kernel.rollback_workflow()` 统一通过 Gateway 推进状态；
   - `bin/herdr-task`（`set`、`supersede`、`_mark_workflow_completed`、`reopen_workflow`）、`bin/herdr-factory`（`_update_workflow_status`）、`services/herdr-sentinel.py`（看门狗超时）、`herdr/steering.py`（紧急中断与 halt）全量收敛至 Gateway。
4. **单向跨进程锁定投影同步与严格解析优先级**：
   - 所有兼容性 JSON 导出（`tasks.json` / `workflows.json`）通过 `sync_tasks_projection` / `sync_workflows_projection` 统一在 `.{filename}.lock` 排他锁内从 SQLite 最新状态重导出后原子写入，杜绝旧快照覆盖更新；
   - 投影路径通过 `resolve_tasks_projection_file` 与 `resolve_workflows_projection_file` 解析，严格保证显式参数与环境变量优先。
5. **门禁前置、Teardown 所有权预占与 CLI 自闭环**：
   - `herdr-task set <task> <status>` 遇未知任务/状态保持 exit 1，遇非法转移保持 exit 2；同状态仅更新 verdict/note 时走 `update_task_metadata()`，不产生假事件；
   - `herdr-task close-workflow` 严格执行 **Gate before Side Effect** 与 **Ownership Acquisition**：在任何 finalize/tab close 前先做状态跃迁前置校验，并原子推进为 `closing` 状态；默认只允许 `running`/`in_progress` 正常流转，物理销毁完成后最终落库 `completed`。
6. **元数据隔离更新与状态保护**：
   - 严禁通过 `save_tasks` / `save_workflows_data` 全量快照更新部分属性；
   - 凡涉及 `stage_verdict`、`commit`、`integration_*`、`paused_nodes`、`gate_overrides`、`history` 等元数据变更，必须调用 `update_task_metadata()` / `update_workflow_metadata()`；
   - 元数据接口对 `status`、`task_id`、`workflow_id` 等核心身份与生命周期字段执行强制保护拦截，违规即报 `ValueError`。
7. **事件审计流防篡改与实体合法性兜底**：
   - 定义 `RESERVED_EVENT_METADATA_FIELDS = {"from", "to", "from_status", "to_status", "reason", "source", "timestamp", "forced"}`，双重杜绝审计日志伪造；
   - `save_task` 自动确保的父工作流初始状态严格置为 `"pending"`，严禁 `"unknown"`；
   - 独立常驻进程（`herdr-sentinel.py`）启动显式引导根目录 `sys.path`。

### 验证命令 / 证据

```bash
# 1. Gateway 契约与并发回归测试（32 项：规则、非法拒绝、事务回滚、Admin force、事件流、Fail-Closed、防倒灌、投影锁、Teardown 门禁前置、并发元数据状态防踩踏、closing 状态防 TOCTOU 竞态、投影环境变量优先级、保留事件字段防伪、Sentinel 独立启动 bootstrap、已完成 Task verdict 纯元数据更新防假跃迁、自动补全父工作流 pending 状态）
pytest tests/test_state_transition_gateway.py -v

# 2. 全仓 422 项自动化测试全量回归
pytest -q

# 3. 生产服务体检
./bin/herdr-factory doctor
```

---

## 37. 仓库根目录重命名与品牌迁移后的运行时路径断裂陷阱：特征指纹定位原则与彻底根除假象兼容

### 问题背景

在品牌统一升级为 HAFlow 并将本地代码主目录从 `~/herdr` 重命名为 `~/HAFlow` 后，Web 控制台点击「执行者自检」报错：
`Deep Preflight 未安装: /Users/user/herdr/herdr/deep_preflight.py`。
排查发现：
1. **控制台根目录解析失效**：`console/herdr_factory_console.py` 中的 `_resolve_herdr_root()` 仅检测 `(candidate / "herdr").is_dir()`。因 Python 顶层包目录就叫 `herdr`，当用户在家目录下存在旧目录或兼容软链接 `/Users/user/herdr` 时，候选路径命中 `/Users/user`，导致计算出的路径为 `/Users/user/herdr/deep_preflight.py` 而非实际源码树 `/Users/user/HAFlow/herdr/deep_preflight.py`；
2. **软链接掩盖真实根因（兼容假象）**：此前建立的 `/Users/user/herdr -> ~/HAFlow` 软链接掩盖了路径迁移不彻底的事实，导致多处守护进程（LaunchAgents）、CLI 脚本（`bin/herdr-task`、`bin/herdr-factory`）、服务（`services/herdr-controller.py`）以及持久化配置（`projects.json` / `workflow.json`）继续引用旧路径，系统处于严重的路径裂脑状态；
3. **常驻守护进程脱离源码树**：控制台守护进程运行于 `~/.herdr-console/`，仅修改代码仓库文件而不执行 `install-herdr-console.sh` 不会生效。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| **单层目录名判定导致根路径误判** | 仅按 `(candidate / "herdr").is_dir()` 检测极易被父目录下的同名包子目录或软链接欺骗 | 采用复合特征指纹校验：必须同时满足 `(candidate / "herdr" / "__init__.py").exists()` 且 `(candidate / "bin").is_dir()`，严格锁定源码根 |
| **软链接制造虚假兼容性** | 临时软链接虽能解燃眉之急，但会导致配置与日志中陈旧路径持续蔓延与沉淀 | 重构/改名必须物理彻底断开旧路径，全盘清理（Grep-Purge）并移除所有软链接拐杖，迫使所有组件面向新标准路径或动态探针自愈 |
| **多环境常驻进程分发落后** | 部署于用户主目录或系统级 LaunchAgents 的服务脱离 Git 工作区，直接改动仓库代码不会自动热加载 | 服务脚本修改后必须前置重新分发（如执行 `./scripts/install-herdr-console.sh` 并 `launchctl kickstart -k`），且需建立自检闭环 |
| **持久化状态路径漂移** | JSON 数据库与任务状态文件中固化了绝对路径，换目录后可能成为暗雷 | 运行时配置中的路径尽量采用相对项目根或动态通过项目名重新解析，防止母体移动后子工位引用悬空 |

### 操作规范

1. **精准特征指纹探针**：
   在 `console/herdr_factory_console.py` 中重构 `_resolve_herdr_root()`：
   优先级：`os.environ.get("HERDR_ROOT")` -> 逐级向上回溯判定 `(p / "herdr" / "__init__.py").exists() and (p / "bin").is_dir()` -> 优先扫描已知标准目录 `Path.home() / "HAFlow"` -> 备选 `Path.home() / "herdr"`。
2. **全系统硬编码旧路径清剿**：
   - 彻底删除 `/Users/user/herdr` 软链接；
   - 全盘将 `bin/herdr-task`、`bin/herdr-factory`、`services/herdr-controller.py` 中的 `~/herdr` 替换为 `~/HAFlow`；
   - 同步更新 LaunchAgents plist 文件（`com.user.herdr-controller`, `notifier`, `sentinel`）；
   - 更新 `~/.zshrc` 中的 `PATH` 指向 `/Users/user/HAFlow/bin`；
   - 更新持久化元数据（`~/.herdr-controller/projects.json`、`workflow.json`）中的 `project_root`。
3. **控制台服务部署与重启契约**：
   涉及 `console/` 任何改动，必须显式调用 `./scripts/install-herdr-console.sh`，确保二进制同步部署至 `~/.herdr-console` 并热重载 launchd。

### 验证命令 / 证据

```bash
# 1. 验证全仓再无旧绝对路径残留
grep -rn "/Users/user/herdr" .  # 期望输出为空

# 2. 控制台动态解析单元与语法回归
pytest tests/test_console*.py -v

# 3. 全仓回归测试（422 项全绿通过）
pytest -q

# 4. 执行者自检功能端到端验证
curl -s http://127.0.0.1:8765/api/preflight/deep | grep '"installed": true'
```


---

## 38. 健康探针“一刀切超时 + 窄分类 + 丢证据”导致的执行者自检误判：按执行者校准超时、分类必须兜底、结论必须附证据

### 问题背景

Web 控制台「执行者自检」（`herdr-deep-preflight --deep`）报告 `opencode 错误 真实最小调用失败 code=1 (1.65s)` 与 `claude 超时 35s`，但手工复测两路执行者实际可用。排查确认三重 compounding 误判：

1. **统一超时阈值误杀慢执行者**：`smoke_probe` 对所有 Agent 一刀切 `timeout=35`，而实测 `claude --print` 冷启动一次 36.9s 才成功、另一次 60s 仍无输出——健康但慢的执行者被确定性误判为 `TIMEOUT`（手册里写的 25s 错得更离谱）；
2. **分类模式过窄导致快失败无归因**：`TOKEN_PATTERNS` / `AUTH_PATTERNS` 仅覆盖英文基础措辞，`opencode run` 1.65s 启动期快失败的常见措辞（401/402、billing/premium 限额、overloaded/5xx/连接失败、模型不存在）全部漏判，落入无信息量的通用 `ERROR`；
3. **控制台弹窗丢弃原始输出**：`runPreflight` 弹窗只渲染 `note + adapter`，丢掉 `deep.output`——用户看到结论却看不到证据，无法复核分类是否准确，形成“自检不准”的体感。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| **单一样本定时判决** | 一次超时就判死刑，混淆了“慢”与“不可用”；冷启动抖动大的 CLI（claude）必然被冤杀 | 超时阈值必须按执行者分别校准（`SMOKE_TIMEOUTS`），抖动大的探针超时后自动重试 1 次；`TIMEOUT` 永不触发 `--auto-disable` |
| **英文窄模式分类器** | 只写 happy-path 英文正则，真实世界的 provider 错误措辞（计费/过载/5xx/连接）必然漏网 | 分类模式库必须覆盖 401/402/403、billing/credits、overloaded/5xx/connection/model-not-found；新增类别（如 `PROVIDER_ERROR`）要同步更新所有消费者（控制台 label、print_table、auto_disable 集合） |
| **结论与证据分离展示** | 探针返回了 `output` 但 UI 不展示，等于没有证据 | 任何健康结论的 UI 必须同时渲染原始输出尾部（本例 800 字符 `<pre>`），让人工可复核 |
| **调用方总超时落后** | 探针侧超时放宽后，控制台 180s 总超时兜不住 `90s×重试 + 串行多路` 的最坏链路 | 放宽探针超时必须同步放宽所有同步调用方的总超时（本例控制台 180s→320s），并更新断言该超时值的单测 |

### 操作规范

1. 在 `herdr/deep_preflight.py` 中维护 `SMOKE_TIMEOUTS`（当前仅 `claude: 90`）与 `DEFAULT_SMOKE_TIMEOUT = 40`；新增抖动大的执行者时优先加超时 + 重试，而非放宽全局阈值；
2. 新增 `final_status` 枚举值时，必须同步三处：控制台 `statusLabel` + `hard` 集合、`print_table` 的 `bad` 集合、`main` 的 `auto_disable/hard` 集合；
3. 涉及 `console/` 任何改动，必须执行 `./scripts/install-herdr-console.sh` 并 `launchctl kickstart -k` 热重载（见 §37.3），否则线上仍是旧逻辑；
4. 探针口径变更必须同步 `docs/operations/deep-preflight-playbook.md` 超时表与 `wiki/preflight-and-health.md` §3.2，并在 `wiki/log.md` 追加演进记录。

### 验证命令 / 证据

```bash
# 1. 新回归测试（分类/超时/重试/证据展示 9 项）
pytest tests/test_deep_preflight_accuracy.py -v

# 2. 全仓回归（431 passed）与编译检查
pytest 2>&1 | tail -n 2
python3 -m compileall -q herdr/ services/ bin/ tests/ console/

# 3. 浅层口径未回归
./bin/herdr-deep-preflight --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['mode'], [(a['agent'],a['final_status']) for a in d['agents']])"

# 4. 手工校准依据（claude 冷启动耗时）
time (timeout 60 /Users/user/.volta/bin/claude --print "Reply with exactly HERDR_PREFLIGHT_OK and nothing else.")
```

---

## 39. 探针分类必须区分“远端拒绝 / 本地崩溃 / 未覆盖”：LOCAL_ERROR 与 pi 适配器的复核闭环

### 问题背景

§38 修复上线后，用户复测报三条新结果：`qodercli code=1 (19.73s)`、`opencode PROVIDER_ERROR (1.16s)`、`pi 未知`。逐路复现抓输出后结论各不相同：

1. **qodercli 是 CLI 本地崩溃**：`Watcher did not become ready within 5000ms: ~/.qoder-cn/skills`（bun 文件监视器启动失败），与模型/凭证/配额完全无关，却被归入无信息量的通用 `ERROR`。且该失败是偶发的（同命令稍后 13.66s 成功）， skills 目录 145 个软链接均无断裂；
2. **opencode PROVIDER_ERROR 是真阳性**：1.16s 快失败命中服务端拒绝模式，5 次复测全部 6–13s 成功——免费共享模型的过载抖动，探针如实报告了那一刻的真相；
3. **pi 未知掩盖了真问题**：`pi --help` 明确文档化 `-p/--print` 非交互模式 + `--no-session`  ephemeral 开关，具备安全探针条件；接入后 live 深探直接打出 `AUTH_REQUIRED`（api key invalid）——此前浅层体检因“认证文件存在”一直报 READY，恰是文件存在≠凭证有效的误报。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| **本地崩溃混入通用 ERROR** | watcher/ENOENT/EACCES 这类 CLI 自身基础设施故障与远端拒绝的止血动作完全不同，混在一起误导排查方向 | 新增 `LOCAL_ERROR` 类别（仅匹配致命启动签名；注意 bare `skill conflict` 在成功输出里同样出现，不可匹配）；计入建议禁用、不触发 `--auto-disable` |
| **快失败值得一次廉价重试** | 过载/503 类拒绝来得快（~1s），单样本易把抖动判成中断；但慢失败已花掉时间预算，不值得再花 | 仅对耗时 ≤15s 的 `PROVIDER_ERROR` 重试 1 次；慢失败与通用 `ERROR` 保持单样本（qoder watcher 每次 ~20s，重试纯浪费） |
| **UNKNOWN 是债务不是状态** | “暂无安全适配器”长期挂着，等于放任该执行者永远未经真实校验 | 每个 UNKNOWN 都必须有消除计划：核查 `--help` 确认非交互开关后立即接入（如 pi `--print --no-session`），并用一次 live 深探验证分类链路 |
| **终端与控制台结论打架先查环境** | 同一 pi 在终端 401、控制台 READY——实为终端 `DEEPSEEK_API_KEY` 已过期（尾部 `4a3d` 与报错掩码一致），遮蔽了文件中的有效凭证；LaunchAgent 精简环境反而用了对的凭证。两边探针各自正确，错的是被污染的环境 | 自检结论不一致时，先 `env \| grep` 比对可疑 key 后缀与报错掩码，再用 `env -u <VAR> <probe>` 隔离验证；过期 key 立即轮换或 unset |

### 操作规范

1. 新增 `final_status` 枚举值时同步四处：`print_table` 的 `bad` 集合、控制台 `statusLabel` + `hard` 集合、`auto_disable/hard`（偶发类不进）、`choose_smoke_command`/分类器单测；
2. 为新执行者写适配器前，必须通读其 `--help` 确认非交互开关的副作用（`--no-session` / `--no-session-persistence` 类开关优先），先手工跑通再接入；
3. 用户报告自检异常时，先复现抓 `output` 原文再下结论——本轮三条报告对应三种不同真相（本地崩溃 / 真阳性抖动 / 覆盖缺失），不可一概而论。

### 验证命令 / 证据

```bash
# 1. 新回归 15 项（含 LOCAL 分类、pi 适配器、快失败重试/慢失败单样本）
pytest tests/test_deep_preflight_accuracy.py -v

# 2. Live 端到端深探（claude 39.45s READY 反证旧 35s 阈值必误杀；pi 打出 AUTH_REQUIRED 真问题）
./bin/herdr-deep-preflight --deep --json

# 3. 终端与服务结论不一致时，隔离可疑环境变量复测
env -u DEEPSEEK_API_KEY /opt/homebrew/bin/pi --print --no-session "Reply with exactly HERDR_PREFLIGHT_OK and nothing else."
```

---

## 40. 兼容投影的写入必须经统一同步器：一次测试环境隔离缺失覆盖了线上 42 条工作流

### 问题背景

用户报告 `wf-xiyu-bid-poc-0915-01`（应答片段摘要优化）在控制台"过一会儿就完全找不到"，但终端现场仍在、后端仍在运行（卡在需求分析）。

逐层取证定位到两个独立问题，本条记录第二个（数据层）：

1. 卡住：`requirements` 节点下两个任务必须全部终态才算节点完成（`services/herdr-controller.py: is_node_complete` 的 `all()` 语义），一个 qodercli 任务停在 `dispatched`（prompt 投递未达 `working`），整条 DAG 被阻塞——这是设计内门禁，非缺陷；
2. 丢失：SQLite `state.db` 与 `tasks.json` 完好（42 条工作流 / 159 条任务），但 `workflows.json` 只剩 1 条测试工作流 `wf-e2e-no-json-01`（其 `workflow_file` 指向 pytest 临时目录）。控制台工作流列表/详情全部读取 `workflows.json`，一份被测试污染的文件直接把整个项目的 UI 抹黑。

污染链：`herdr/projects.py`、`herdr/agent_router.py`、`bin/herdr-factory` 的读取统一走 `_get_store()`（感知 `HERDR_STATE_DB` / `WORKFLOWS_FILE`），但写入兼容投影时直接 `_save(WORKFLOWS_FILE, ...)` 硬编码 `~/.herdr-controller/workflows.json`；`tests/test_state_store.py::test_end_to_end_single_source_of_truth_without_workflows_json` 只设了环境变量、未 `monkeypatch.setattr` 模块全局量，pytest 跑一次就把线上 `workflows.json` 覆盖成测试投影。另 `save_workflows()` 还是"按传入子集全量重写文件"，本身即覆盖向量。

2026-09-23 再次发现同类读取分叉：普通工作流页面与执行者负载经 `tasks()` 读取 `tasks.json`，而运维驾驶舱通过 `herdr-task ops-center` 读取 StateStore。对 `wf-haflow-0923-01`，普通接口返回 `tasks=[]`，SQLite 中有任务，兼容 `tasks.json` 中却没有该 Workflow 的记录。StateStore 单一事实源改造于 2026-09-13（`d876f27`）；Console 的通用任务读取入口当时仍读投影，因此阶段、任务卡和执行者负载都可能显示为零。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **读写路径不一致** | 读走环境感知的 StateStore、写走硬编码路径——只要有一处写路径不感知环境，测试/沙盒进程就能静默改线上数据 | 投影只是 SQLite 的只读镜像；所有写入收敛到唯一的 `sync_*_projection()`，模块内严禁再出现 `_save(硬编码 WORKFLOWS_FILE)` |
| **以子集全量重写** | `save_workflows(data)` 把调用方传入的部分数据整体写成文件，天然丢数据 | 投影导出永远从 SQLite 全量导出，禁止"局部数据 + 全量覆盖" |
| **测试隔离只做一半** | 只设环境变量、不 patch 模块全局量，隔离就是纸糊的 | 测试隔离双保险：环境变量 + `monkeypatch.setattr(module, "FILE", tmp)`；再用"线上文件字节不变"断言防回归 |
| **UI 主数据源脆弱** | 控制台以兼容投影为主数据源，投影一坏 UI 全黑，且现场难以自证 | 关键读取优先 StateStore；投影损坏可一行 `sync_workflows_projection` 从库重放（本次演练恢复 42 条） |
| **Console 多个任务视图读了旧任务投影** | `tasks()` 被工作流详情、执行者负载、工位占用和任务详情复用，但它从 `tasks.json` 读取；StateStore 中的新任务因此不会进入这些视图 | 将共享 `tasks()` 接到 `herdr_kernel.load_tasks_data()`；调用者再按 Workflow、项目或任务 ID 过滤，JSON 只作兼容投影 |

### 操作规范

1. 任何写 `workflows.json` / `tasks.json` 的代码，必须经 `herdr/state_store.py` 的 `sync_workflows_projection` / `sync_tasks_projection`；PR 审查重点 grep 硬编码直写；
2. 触发写操作的测试必须同时隔离 env 与模块全局量；回归用例 `test_workflow_writes_never_touch_real_projection_without_global_patch`（仅 env 隔离时断言线上文件字节级不变）纳入全量套件；
3. 现场恢复 SOP：投影与库不一致时，先备份 `workflows.json`，再从 SQLite 重放投影，严禁反向以投影覆盖库。
4. Console 的任务视图统一通过 `tasks()` 读取 StateStore；新增读路径不得直接打开 `tasks.json`。回归测试同时断言投影为空时 Workflow 任务仍可见、执行者负载正确，并且任务不会跨 Workflow 串入。

### 验证命令 / 证据

```bash
# 1. 修复前复现：仅 env 隔离下 probe 穿透写入线上 workflows.json（复现后已恢复现场）
# 2. 修复后回归：单测 14 项 + 全量 432 项通过
pytest tests/test_state_store.py -x -q
pytest -q 2>&1 | tail -n 2
pytest -q tests/test_console_project_creation.py::ConsoleWorkflowStagesTest::test_tasks_for_workflow_reads_state_store_not_json_projection
# 3. 现场恢复演练：从 SQLite 重放投影，42 条工作流全部回到控制台可见
python3 -c "from herdr.state_store import get_state_store,sync_workflows_projection; sync_workflows_projection(store=get_state_store())"
# 4. 投影与库一致性抽查
python3 -c "import json; from herdr.state_store import get_state_store; print(len(get_state_store().list_workflows()), len(json.load(open('$HOME/.herdr-controller/workflows.json'))['workflows']))"
```

---

## 41. 控制面必须 SLA 化：无界等待 + 静默丢弃 + 输入不可信 = 任何瞬态异常都会变成永久卡死

### 问题背景

`wf-xiyu-bid-poc-0915-01` 在 implementation→test 边界卡死 6.5 小时，用户称之为"第 N 次临时救援"。全量日志取证得到一组触目惊心的数字（均为 `~/.herdr-controller/logs` 实测）：

| 现象 | 证据 |
|---|---|
| 单条事件把调度通道锁死 5.25h | `controller.out.log:593749-595007` 连续 1,259 行 `[COORDINATOR BUSY]`；frontend `agent_done` 00:17 入队，06:51 才送达 |
| `interrupted` 状态死区 6h47m | backend 任务 00:06 进入 `interrupted`，`handle_event` 对 interrupted/paused 无任何分支，期间 working/done 翻转 5,572 次全被无视 |
| 夹具 workflow 空转约 20h | pytest 临时目录里的 `wf-e2e-no-json-01`（协调器 pane `pane-coord-1` 不存在）产生 73,383 次 WAIT + 73,383 条 err |
| 僵尸 pane 订阅风暴 | 已消失的 `w6:p1M` 被每 2s 重订阅：128,321 次 `[SUBSCRIBED]` + 128,341 条 `agent_not_found` |
| 完成日志风暴 | 已关闭工作流每 2s sweep 重刷 `[WORKFLOW COMPLETE]` 共 164,398 行 |
| 哨兵完全失明 | 6.5h 卡死期间哨兵零动作（其巡检只覆盖 pane 完成标记与崩溃特征）；历史上 38 次 "Controller restarted for recovery" 反而重启放大风暴 |

直接根因（本次事故）：opencode 的 lifecycle 集成**从未安装**（`herdr integration status` → `not installed`），herdr 退化为屏幕 manifest 探测；pane 缓冲区残留一行 `• Working (26s • esc to interrupt)` 文本被 `interrupt_hint_working` 规则持续命中，总指挥 agent 状态被永久锁死为 `working` —— 而 Controller 的唯一投递条件是总指挥 idle。

系统性根因：**HAFlow 控制面对 Actor 的所有等待都是无界的，事件投递没有失败语义，输入（agent 状态）不可信，且没有任何一层对"停滞"本身负责。**

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **无界等待** | 任何 `while True + sleep` 等待 Actor 的循环，都会在 Actor 异常时变成永久阻塞；控制面不存在"等多久都合理"的等待 | 所有 Actor 交互必须有 SLA（`herdr/liveness.py`），到期记录 attention 并让位，慢速重试 |
| **静默丢弃** | `[QUEUE STALE]`、投递失败后 key 被 discard，blocked 事件投递失败后没有任何补投机制——事件"入过队"不等于"送达" | 事件投递必须幂等可重试：attention episode 记录 attempts/next_retry_at，watcher 按退避补投 |
| **状态死区** | 状态机的合法中间态（interrupted/paused）如果没有驱动者，就是事实上的永久卡死；`handle_event` 只处理 4 种状态，其余全部悬空 | 每个"等待裁决"的状态必须有超时升级机制（attention 事件 → 总指挥必须裁决） |
| **输入不可信** | agent 状态靠屏幕正则猜测时，一行历史残影即可永久误判；集成缺失必须在启动时大声暴露，而不是让人 6 小时后人工发现 | 启动执行 `herdr integration status` 健康检查并打印 `[INTEGRATION GAP]`；排障第一步 `herdr agent explain` |
| **重试风暴** | 无退避的 2s 重试会制造 10 万级日志与 CPU 空转；重启（38 次）会重放风暴 | 所有重试指数退避封顶（2s→…→300s），重复性日志加单次闩（完成/放弃只允许打印一次） |
| **夹具污染调度** | 测试夹具 workflow（pytest tmp / 无项目空壳）进入生产 sweep 后，会对不存在的 pane 无限重试 | sweep 源头过滤夹具指纹（`pytest-*`、/tmp、已删除的 workflow_file、无 project_id 空壳） |

### 操作规范

1. **禁止新增无界等待**：任何等待 Actor 的新代码必须使用 `herdr/liveness.py` 的 SLA/退避策略（`coordinator_delivery_sla` / `stage_advance_sla` / `backoff_delay`），PR 审查重点 grep `while True` + `time.sleep`；
2. **事件不可静默丢失**：投递失败/停滞必须写入 attention episode（`~/.herdr-controller/attention.json`），由 registry watcher 按 `next_retry_at` 慢速补投；成功送达即清除；
3. **状态机无死区**：新增/使用中间态（interrupted、paused 等）必须同时提供超时升级路径（本次为 attention 事件 + 总指挥强制裁决指令）；
4. **重试必须退避 + 封顶 + 单次告警**：订阅退避基线见 `liveness.subscribe_*`；`[LISTENER GIVEUP]`、`[WORKFLOW COMPLETE]`、`[WORKFLOW FOREIGN SKIPPED]` 均只允许出现一次；
5. **集成健康是启动门禁**：Controller 启动即检查在用 agent 的 herdr 集成，缺失打印修复命令 `herdr integration install <kind>`；
6. **哨兵补齐停滞盲区**：Sentinel 新增 `[SENTINEL STALL]` 巡检（默认 30 分钟无状态推进即告警 + macOS 通知），不再只盯 pane 完成标记。

### 验证命令 / 证据

```bash
# 1. 新增 Liveness Guard 回归（22 项：SLA/退避/卫生/attention/停滞检测）
pytest tests/test_liveness_guard.py -v

# 2. 全量回归
pytest -q                                   # 期望 476 passed

# 3. 现场验证：夹具 workflow 被排除、真实 workflow 正常调度
rg "WORKFLOW FOREIGN SKIPPED|STAGE ADVANCE QUEUED" ~/.herdr-controller/logs/controller.out.log | tail

# 4. 集成健康（本事故根因）
herdr integration status | rg "not installed"
herdr agent explain w9:p1                   # 期望 screen_detection_skip_reason: full_lifecycle_hook_authority

# 5. 新 BUSY 日志自带等待时长与升级路径（旧版没有 waited=）
rg "COORDINATOR BUSY|COORDINATOR STALLED" ~/.herdr-controller/logs/controller.out.log | tail
```

---

## 42. 单点 LLM 协调器在热路径上 = 全流程串行税：常规推进必须规则化，等待预算必须对齐真实操作

### 问题背景

上一条事故（§41）修复了控制面的"无界等待"后，`wf-xiyu-bid-poc-0915-01` 的调度不再卡死，但用户仍反馈"跑一条流程比自己做一次任务慢几倍"。对 `~/.herdr-controller/state.db` + `controller.out.log` 做全量取证，数字如下：

| 现象 | 证据 |
|---|---|
| 墙钟 10.0h 中 Agent 真正干活仅 1.18h（12%） | 事件表 union(working 区间)；其中 6.6h 为机器休眠（00:04 display off → 06:39 on，darkwake 每小时一次） |
| 每个节点完成 / 阶段推进都要等总指挥空闲 + 跑完整 prompt 回合（单轮上限 10min） | `[STAGE ADVANCE WAIT]` 78,979 行、`[COORDINATOR BUSY]` 5,706 行；`done→completed` 常见 9-10min 等待 |
| 决策窗口仅 30s，超时后立刻再烧一整轮 | `[DECISION TIMEOUT]` → `[COORDINATOR RETRY]` → `timed out waiting for agent status` |
| fix-loop 门禁 blocked 后整节点重跑 | 12 次 rework；test 节点两个任务全被 superseded 后重派 `-r2`，其中前端测试与后端修复无关 |
| 提交钩子内联跑全量测试 | `[COMMIT ERROR]` 捕获到 staged vitest 全量 stderr；每个 git 集成任务额外 1-13min |
| 阶段切换空档：71min / 14min / 7min / 14min | dispatched→working 最长 77min；节点间全靠总指挥回合衔接 |

直接根因：**协调器是一个 LLM，且被放在每一跳的同步关键路径上**。LLM 回合的分钟级延迟 × 节点数 × 返工次数 = 数倍于实际工作的放大系数。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **LLM 进热路径** | 常规、可规则化的决策（节点完成 → 按模板派发下一节点）交给 LLM 就是为每一步支付分钟级税；LLM 的价值在异常裁量而非例行推进 | 规则化优先、异常回落：controller 直接按节点模板生成 Task，仅配置不足/需求缺失/launch 失败才唤醒协调器 |
| **等待预算与操作不匹配** | 30s 决策窗口对分钟级 LLM 回合必然误判，误判的代价是再花一整轮 | 等待预算按真实操作耗时标定（180s），超时写入 attention 退避而非立即重试 |
| **返工粒度** | 门禁失败按"节点"作废会让无关的通过项一起重跑，成本随重试轮数线性叠加 | 作废以"受影响子集"为准：只重派失败/无结论任务，`verdict=pass` 且已落定的保留 |
| **同步门禁过重** | commit 时跑全量测试与 workflow test 节点职责重复，且把长任务塞进每个任务的收尾热路径 | 门禁分层：commit 快速必需检查、全量测试后置到 test 节点/pre-push/CI；延迟必须以显式开关驱动，禁止按仓库来源猜测（残留标记会误伤人类提交） |
| **统计口径失真** | 机器休眠不属于流程耗时，但会污染"流程变慢"的判断 | 长跑持有 `caffeinate`（活跃 workflow 期），评估时长先剔除 machine sleep |

### 操作规范

1. **阶段推进默认规则化**：`herdr/direct_dispatch.py`（纯函数）负责决策，controller 只做 launch 装配；节点字段为空时与总指挥路径一致回退 `stage-policies.json`（`merge_node_policy`），节点与 policy 都没有 `purpose` 才回落协调器；总开关 `HERDR_DIRECT_STAGE_DISPATCH=0`；
2. **等待预算必须标定**：任何新增 Actor 等待的预算按"真实回合尾部耗时"设置并 env 可覆盖（`HERDR_COORDINATOR_DECISION_TIMEOUT`），超时一律走 attention 退避；
3. **返工作废按子集**：fix-loop 只作废失败任务 + 全部下游；保留项必须"已落定或无需 Git 集成"（`completed+git` 仍走 finalize+作废，防止未提交任务滞留）；
4. **提交门禁延迟显式化**：controller 收尾 commit 下发 `HERDR_DEFER_HEAVY_TESTS=1`，目标仓 hook 只认该显式开关；
5. **长跑防休眠**：controller 在存在活跃非夹具 workflow 时持有 `caffeinate -i -s -w <pid>`（`HERDR_AWAKE_GUARD=0` 关闭），workflow 清零/进程退出自动释放。

### 验证命令 / 证据

```bash
# 1. 决策纯函数 + 控制器装配 + 门禁子集回归
python3 -m unittest tests.test_direct_stage_dispatch tests.test_fix_loop_gates -v

# 2. 全量 unittest（pytest-only 文件需本地安装 pytest）
python3 -m unittest discover -s tests

# 3. 现场验证：常规推进不再出现 STAGE ADVANCE WAIT
rg "STAGE ADVANCED DIRECT|DIRECT DISPATCH FALLBACK|FIX LOOP SUBSET KEEP|AWAKE GUARD|DECISION TIMEOUT" \
   ~/.herdr-controller/logs/controller.out.log | tail

# 4. 目标仓门禁拆分区分度（herdr 克隆 vs 人工）
HERDR_DEFER_HEAVY_TESTS=1 bash scripts/check-testing-standards.sh   # 期望：延迟提示、exit 0
SKIP_TESTING_GATE=1      bash scripts/check-testing-standards.sh   # 期望：正常测试门禁路径
```

## 43. 跨阶段返工拓扑作废盲区与主仓脏树收尾断链死锁

### 问题背景

2026-09-16，在 `wf-xiyu-bid-poc-0915-01` 执行过程中，review 门禁 2 阻断项被触发，回流至 implementation 阶段修复（任务 `fix-review-blockers`）。协调器 OpenCode 验收通过并产生 `completed` 结论，宣告 Controller 即将自动执行 `commit -> integrate -> cleanup` 并推进 `test -> review`。然而系统在此处再次完全停滞，总指挥处于空闲等待状态数十分钟。
现场取证发现两个互为交织的系统性卡点：
1. **主仓脏树阻断 Git 集成，无重试导致任务永久悬挂**：主仓 `xiyu-bid-poc` 本地遗留了未提交的 `scripts/check-testing-standards.sh` 脚本修改（用于支持 `HERDR_DEFER_HEAVY_TESTS=1`，未经 PR 提交或 stash）。`herdr-task integrate` 执行严格的 `git status --porcelain --untracked-files=no` 守卫直接退出 5（`Main repository has tracked changes`），导致任务卡在 `status: committed`。而旧版 Controller 的 `finalize_completed_task` 仅接收 `status == "completed"`，一旦进入 `committed` 重试即被视为非法状态跳过，且主循环无已提交任务的补收尾调度，导致集成彻底断链。
2. **跨阶段返工漏作废中间验证节点**：DAG 拓扑为 `implementation -> test -> review -> wrapup`。当 `review` 门禁回流至 `implementation` 时，旧版 `invalidate_for_fix_loop` 仅以 `gate_node_id`（`review`）作为根节点收集下游闭包，**完全遗漏了 `retry_node` 与 `gate_node_id` 之间的中间节点 `test`**。`test` 阶段上一轮的 r2 任务仍为 `cleaned`，导致 Controller 的 `is_node_complete('test')` 仍为 True。当修复任务完成后，DAG 判定 `test` 已完成，试图直奔 `review`；而 `review` 的 `notified` 锁未解除，导致总指挥永远等不到 `test` 阶段事件。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **返工作废拓扑断层** | 回流到更上游节点时，中间经过的所有验证节点基线已作废，只作废门禁自身必然导致中间节点被"幽灵跳过" | 回流作废闭包必须包含 `retry_node` 的全部下游节点（排除 `retry_node` 自身），即 `(downstream(retry_node) - {retry_node}) ∪ downstream(gate_node)`，且跨阶段回流时中间节点不可复用 |
| **收尾操作缺乏幂等重试** | 分布式集成容易受锁、工作区脏树、网络抖动等偶发干扰；一旦中间态（如 committed）不可重入，偶发故障即变成永久死锁 | `finalize` 必须对 `completed` 与 `committed` 幂等：若已 committed 则跳过 commit 直接重试 integrate 与 cleanup；Registry Watcher 必须为 committed 态设置 attention 慢速重试护栏 |
| **工作区洁净度是集成红线** | 主仓库脏工作树会导致 `git switch` 与 `ff-only` 合并失败或污染现场 | 跨仓脚本变更必须通过规范沙盒分支提交合入，严禁直接在主仓库工作区修改而不提交/不暂存 |

### 操作规范

1. **跨阶段返工作废闭包**：`invalidate_for_fix_loop` 显式接收 `retry_node`，计算拓扑区间并联动作废；当 `retry_node != gate_node_id` 时，中间节点（如 test）的所有历史任务一律标记 `superseded`；
2. **收尾幂等化**：`finalize_completed_task` 状态检查放宽为 `status in ("completed", "committed")`；
3. **Committed 状态巡检自愈**：Registry Watcher 自动探测滞留在 `committed` 的 Git 集成任务，以 attention 退避周期触发补收尾，故障自愈后无需人工干预；
4. **主仓环境规范**：严禁在作为 Git Anchor 的宿主主仓直接做未提交改动。

### 验证命令 / 证据

```bash
# 1. 跨阶段中间节点作废与 committed 幂等收尾单元测试
python3 -m unittest tests.test_fix_loop_gates.InvalidateFixLoopSubsetTest.test_intermediate_nodes_invalidated_when_gate_retries_upstream_node -v
python3 -m unittest tests.test_fix_loop_gates.FinalizeAlreadyCommittedTaskTest -v

# 2. 全量回归测试
pytest -v tests/test_fix_loop_gates.py

# 3. 现场验证：wf-xiyu-bid-poc-0915-01 自动推进并派发 r3/r4
herdr pane read w9:p1 --lines 30
```

---

## 44. 多会话并行必须 CoW 沙盒隔离：共享主工作区没有"我的改动"边界

### 问题背景

2026-09-16 控制面延迟优化（§42）落地期间，同一台机器上两个 AI 会话同时操作 `/Users/user/HAFlow` 与 `/Users/user/xiyu/xiyu-bid-poc` 的主工作区：

- HAFlow：第二次 `git add services/herdr-controller.py` 时，把并行会话尚未提交的 fix-loop 改动一起打进了 PR 提交；随后对方又对 lessons/tests/wiki 写入在途内容，同一文件上出现两个写者的叠加；
- xiyu：本会话在 `main` 工作区留下的未提交 hook 改动，被工作流自身的分支切换直接冲掉（工作区切到 `herdr/wf-…-consolidated` 后改动消失），只能从零重建。

两起事故的共同根因不是 git，而是流程违反 RULES.md §2「CoW 沙盒建支红线」与 unified-dev-flow S0：**在一个多写者共享的主工作区里直接开发**。`git add` 的粒度是文件，不是"会话归属"；未提交改动是工作区级共享状态，任何 `checkout/reset` 都会将其覆盖或丢弃。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **主工作区多写者** | 多个会话/agent 同写一个 checkout 时，按文件 add 必然可能卷走他人在途改动；共享工作区不是工作单元 | 一个工作区同一时刻只允许一个写者；非平凡改动必须在 CoW 沙盒或独立 checkout 进行 |
| **未提交改动是易失的** | 工作区级未提交内容会被他人 `checkout -f`/`reset` 静默冲掉，没有 undo 通道 | 任何有价值改动必须先落在隔离分支/沙盒内提交，再考虑合并，禁止把它留在共享工作区过夜 |
| **CoW vs worktree** | git worktree 共享 refs/index 锁且分支互斥，不能作为并发隔离；APFS `cp -cR` 秒级克隆 + 独立 `.git` 才是物理隔离 | 并发场景一律 CoW 沙盒；worktree 仅在同分支协作明确时使用 |
| **污染的处置** | 已推送提交混入他人 WIP 时，改写历史/强推会破坏对方基线；正确做法是新增 commit 剥离范围并保留对方改动 | 禁止 force push；污染处置 = 范围剥离 + 原样保留 + 明确通知归属 |
| **归属审计** | `git add <file>` 不校验 hunk 归属，同一文件有并行写入时肉眼不可靠 | 提交前 `git diff --cached` 逐 hunk 校对；提交后 `git diff main...HEAD` 做范围复核 |

### 操作规范

1. 任何非平凡改动先建 CoW 沙盒：`cp -cR <repo> ~/.sandboxes/<task-id>`（独立 `.git`，可随意 `reset --hard`/`clean -fd`），完成 push 后 `rm -rf` 清理，零 git residue；
2. 一个工作区一个写者：发现他人在途改动时停手确认，不得"顺手提交"；
3. 提交前审计：`git diff --cached` 逐 hunk 确认归属，尤其同一文件可能有并行编辑；
4. 污染处置固定动作：剥离 commit（禁止 force push / 禁止 `reset --hard` 覆盖他人改动）+ 对方改动原样留在工作区 + 明确通知归属；
5. 发布前在沙盒内干净 checkout 上复验（测试/编译通过）再 push；目标仓专属流程（如 xiyu `scripts/pr-create.sh` + pre-push gate）在沙盒内执行。

### 验证命令 / 证据

```bash
# 1. CoW 沙盒（APFS 秒级，独立 .git）
cp -cR /Users/user/HAFlow ~/.sandboxes/<task-id>
git -C ~/.sandboxes/<task-id> checkout <branch> && git -C ~/.sandboxes/<task-id> status --short

# 2. 提交范围审计（本案使用）
git diff --cached                                  # 逐 hunk 归属
git diff main...HEAD -- <file> | rg "外来讲号" || echo "no foreign hunks"
git diff main...HEAD --stat

# 3. 沙盒内复验后 push，最后清理
python3 -m unittest <相关套件>
rm -rf ~/.sandboxes/<task-id>
```

---

## 45. 工作流多 Agent 协同收敛、双工位对抗审查与跨阶段 Agent 隔离

### 问题背景

在长期的多 Agent 工作流实践中，开发标准流程模板 `software-development-v1.yaml` 存在严重的“无序切碎”与“自审自查”问题：
1. **Pane 终端分屏爆炸**：各节点默认声明“同一阶段允许多个 Task 并行协作”，导致协调器与总指挥在需求分析、架构计划甚至收尾阶段无序切出 3~4 个细碎 Task/Pane，使单个 WezTerm 标签页拥挤不堪，引发严重的上下文碎片化、LLM 协调等待与调度延迟；
2. **缺乏对抗性质询**：需求与计划若仅靠单 Agent 起草，极易遗漏隐式假设、极端边界与架构死锁，缺乏系统化的“红蓝对抗”与漏洞挖掘机制；
3. **实现阶段盲目并发**：未根据代码解耦性动态判断，强耦合模块强行切碎多 Agent 修改同一组核心文件，引发灾难性 Git 合并冲突；
4. **评审与测试自审自查盲区**：测试与评审阶段未与实现阶段进行 Agent 隔离，导致实现者（如 Codex）自己评审/测试自己编写的代码，产生确认偏差与盲区。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **Pane 数量失控** | 开放式并行提示词会导致 LLM 倾向于无限切碎工位，带来巨大的 UI 与协调开销 | 每个节点必须明确硬性设定 `max_agents` 上限；除实现阶段外坚决杜绝 >2 工位 |
| **单视角思维盲区** | 需求与计划若无专门的对抗角色，漏洞往往要流转到下游甚至线上才暴露 | 需求与计划阶段强制收敛为**严格双工位**（`max_agents: 2`）：1 个主执行者 + 1 个对抗性质询者，两份互补交付物完备后方可通过门禁 |
| **强耦合代码并发冲突** | 任务并发必须建立在“文件集合完全解耦”的前提下，强耦合代码并发只会制造合并灾难 | 实现阶段采用**自适应并发**（上限 3）：解耦任务并发，强耦合或单点改动强制单 Agent 顺序执行 |
| **裁判与运动员同体** | 同一 Agent 往往具备相同的认知盲点，无法有效指出自身代码的隐性架构缺陷 | 测试、评审与收尾阶段强制**跨阶段硬隔离**（`exclude_stage_agents: [implementation]`），调度器自动剔除实现者，由跨模型独立把关 |

### 操作规范

1. **工位上限与双工位规范**：`software-development-v1.yaml` 的需求与计划阶段设置 `max_agents: 2`，声明 `roles: [executor, challenger]`，分别输出核心规格与《对抗审查与边界漏洞清单》；
2. **规则化角色直接派发**：`herdr/direct_dispatch.py` 支持解析 `roles`，常规推进直接生成双工位 Task 规格，免除协调器回合等待；
3. **调度器跨阶段硬隔离**：`herdr/agent_router.py` 的 `choose_agent` 解析 `exclude_stage_agents` 策略，自动查询并剔除对应阶段已分配的 Agent，且在单 Agent 受限环境下提供优雅降级保护；
4. **单工位独立验收**：测试、评审与收尾阶段严格限制为单工位（`max_agents: 1`, `parallel: false`），杜绝 Pane 泛滥。

### 验证命令 / 证据

```bash
# 1. 跨阶段 Agent 隔离测试
pytest tests/test_agent_router_stage_exclusion.py -v

# 2. 规则化双工位派发测试
pytest tests/test_direct_stage_dispatch.py -v

# 3. 模板规格与 DAG 合法性测试
pytest tests/test_software_development_v1_template.py -v

# 4. 全仓自动化回归
pytest
```

---

## 46. 异构 AI Agent CLI 全链路接入与工位沙盒信任规范：从解析、协议适配、无副作用探针到工作区准入

### 问题背景

在接入新的异构 AI Coding Agent（如 xAI 的 Grok CLI）时，新 Agent 的接入往往不只是在路由列表里加一个名字，而是横跨从底层进程启动到上层 UI/控制台的完整链路。如果缺少系统化的全链路适配规范，会踩入一系列隐蔽陷阱：
1. **进程启动与非交互参数差异**：不同 Agent CLI 的非交互运行与授权机制差异显著。例如 Grok CLI 默认会在执行工具前阻塞等待终端用户确认，自动化调度若不传递 `--always-approve` 会导致工位 TUI 永久卡死；
2. **工作区信任机制隐性阻断**：类 Claude / Grok 等现代 Agent CLI 具备工作区信任机制，会在未信任目录下弹出交互式 Trust Dialog。如果 Worker 在 CoW 创建的临时克隆沙盒中启动该 Agent，而未提前将其写入各 Agent 的信任配置文件（如 `~/.grok/trusted_folders.toml` 或 `~/.claude.json`），工位将直接卡在交互弹窗上，使任务派发永久超时；
3. **健康自检超时与分类器断裂**：探针若无针对新 Agent 的非交互参数分支（如 `-p / --single`），会退化为错误命令或无交互命令挂死，最终导致调度器无法感知其实际可用性（误判为 `UNKNOWN` 或 `MISSING`）；
4. **既有项目池配置陈旧**：已创建项目的 `agent-pools.json` 在初始化时保存了旧有的 `allowed_agents` 列表，即使系统代码支持了新 Agent，老项目预检仍会因池级白名单缺失而忽略该 Agent。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **交互式授权弹窗阻断自动化** | 后台常驻调度 Agent 时绝不能假设 TUI 会有人类交互点击确认 | Worker 启动时必须针对具体 Agent 注入全自动执行参数（如 Grok 注入 `--always-approve`，Claude 注入 `--dangerously-skip-permissions`） |
| **CoW 临时沙盒缺少工作区信任** | 隔离克隆沙盒路径对 Agent 来说属于全新未知目录，必然触发信任拦截 | Worker 装配工位时必须在启动 Agent 前调用 `ensure_<agent>_workspace_trust` 将沙盒路径原子写入对应配置（如 `trusted_folders.toml`） |
| **探针缺乏非交互模式适配** | 用通用 `run` 或盲目拼装命令会导致 CLI 挂起超时并产生假阳性故障 | `choose_smoke_command` 必须基于官方文档确认的安全无副作用单回合参数（如 `grok -p` + `HERDR_PREFLIGHT_OK` 协议标记） |
| **协议层能力模型未声明** | 缺少 AgentAdapter 声明会导致系统将新 Agent 降级到 fail-closed 的 `UnknownAgentAdapter`，彻底禁用制动与插话 | 必须显式实现具体 `AgentAdapter`，明确声明 `supports_interrupt`、`supports_soft_steer`、`supports_resume` 等 capabilities |

### 操作规范（已固化到 `herdr/agent_binary.py`、`herdr/agent_adapter.py`、`herdr/preflight.py`、`herdr/deep_preflight.py`、`services/herdr-worker.py`、`herdr/agent_router.py`）

1. **二进制解析单一事实来源**：在 `herdr/agent_binary.py:AGENT_BINARIES` 注册 CLI 内部名与可执行文件映射，利用 `resolve_agent_binary` 兼容 LaunchAgent 精简 PATH 与 `~/.local/bin` 等目录；
2. **协议适配与能力声明**：在 `herdr/agent_adapter.py` 实现对应 Adapter，准确声明四维 capabilities 并注册别名；
3. **安全沙盒探活**：在 `herdr/deep_preflight.py:choose_smoke_command` 适配最轻量非交互调用，返回 `HERDR_PREFLIGHT_OK` 精确标记；并在 `preflight.py` 中补充 `AUTH_HINTS`；
4. **沙盒信任与免密执行**：在 `services/herdr-worker.py` 实现 `ensure_<agent>_workspace_trust` 与参数自动化放行；
5. **路由矩阵与控制台/模板同步**：在 `agent_router.py`、`software-development-v1.yaml`、`console/herdr_factory_console.py` 中全量同步。

### 验证命令 / 证据

```bash
# 1. 验证轻量静态体检
./bin/herdr-preflight

# 2. 验证能力矩阵
./bin/herdr-task adapters

# 3. 验证单元测试套件
pytest tests/test_agent_adapter.py tests/test_deep_preflight_accuracy.py tests/test_herdr_worker.py -v
```

---

## 47. 控制台实体筛选的人类心智对齐与上下文感知：从 ID 片段输入到级联下拉与自动预选

### 问题背景

控制台任务归档查询（Task Archive Query）上线后，在实际人机协同使用中暴露出严重的操作阻力：
1. **违背人类心智模型的输入设计**：工作流筛选器被设计为让用户手输 `工作流 ID 片段` 的纯文本输入框。工作流 ID 格式为 `wf-xiyu-bid-poc-0915-01` 等 20+ 字符长串，操作员在控制台回顾历史任务时无法、也不应该去人肉记忆这串字符；
2. **缺乏页面上下文感知（Context-Blindness）**：用户往往是在某个具体项目和具体工作流页面发现卡点或需要复盘时，点击右上角的「任务归档」按钮进入该弹窗。然而弹窗默认是全局项目与全部工作流未筛选状态，将其他工作流的所有历史任务一并混杂展示，用户必须重新手动去搜；
3. **缺少实体级联联动与快捷聚焦能力**：项目切换后未动态级联更新对应的工作流候选列表；当用户在全部任务中浏览到某条感兴趣的任务时，也无法一键点击该任务所属的工作流来直接锁定上下文。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **让用户手输机器 ID 片段** | 用户是以业务实体（工作流名称/需求主题）为思维锚点，而不是机器哈希或时间戳 ID | 凡跨实体筛选必须提供 `<select>` 下拉选择框，选项标签统一使用「自然语言标题/主题 (短ID)」格式 |
| **弹窗打开后上下文丢失** | 按钮是在特定的页面语境中被触发的，弹窗不能假设自己处于孤岛 | 模态框打开时必须自动继承当前页面的 `state.projectId` 与 `state.workflowId` 作为默认筛选条件，直出当前现场数据 |
| **首屏异步加载抖动与网络延迟** | 每次打开弹窗都重新发起网络请求拉取已知实体，会造成下拉框短暂空白或闪烁 | 优先复用前端当前已加载的 `state.project.workflows` 进行 0 延迟首屏直出，跨项目切换时再走轻量异步 API 兜底 |
| **后端缺乏轻量元数据接口** | 旧有 `/api/project` 接口捆绑了 tabs/panes/slots 等重 I/O 检查，不适于作为频繁的联动查询源 | 必须拆出零外部 I/O、纯内存转换的轻量元数据路由（如 `/api/workflows?project_id=...`），保证交互毫秒级响应 |

### 操作规范

1. **工作流筛选升级为下拉选择框**：在 `console/herdr_factory_console.py` 中将 `input#arcWorkflow` 替换为 `select#arcWorkflow`，首项设为「全部工作流」；
2. **自动预选与级联**：`showArchive()` 打开时自动读取当前全局 `state` 预选项目与工作流；`onArchiveProjectChange` 监听项目变更并级联刷新工作流选项；
3. **轻量工作流列表接口**：提供 `GET /api/workflows` 路由，支持可选 `project_id` 查询参数，按 `created_at` 倒序返回带 `requirement_subject` 的工作流轻量字典；
4. **归档任务卡片工作流快捷点击**：任务卡片副标题中的 `workflow_id` 渲染为可点击超链接，点击直接调用 `filterArchiveByWorkflow` 完成过滤；
5. **内嵌脚本语法与路由双重守卫**：新增前端模板断言与 `/api/workflows` 路由测试于 `tests/test_archive_query.py`，并在合并前跑全量 `pytest`。

### 验证命令 / 证据

```bash
# 1. 验证归档查询与轻量工作流路由测试
pytest tests/test_archive_query.py -v

# 2. 验证控制台前端语法
pytest tests/test_console_frontend_syntax.py -v

# 3. 验证控制台服务真实路由响应
curl -s "http://127.0.0.1:8765/api/workflows?project_id=xiyu-bid-poc-a380753e"
```

---

## 48. 阶段推进停滞感知（Stall Detection）的生命周期盲区：从无条件任务扫描到终态与拓扑终点感知

### 问题背景

用户在 HAFlow Web 控制台查看已顺利完成且交付归档的历史工作流（例如 `wf-nexusarchive-54433229-20260913-111049`）时，顶部 Attention Banner 赫然弹出高亮告警：
`推进停滞告警 ⚠️ 上一阶段所有任务均已完成，但后继阶段推进悬挂已超 45 秒`，并附带「⚡ 尝试推进阶段」按钮。若用户点击强推，后台直接报 `RuntimeError: 当前没有可手工推进的下一阶段`。
排查发现：
1. **启发式检测缺乏工作流生命周期前置感知（Lifecycle-Blindness）**：`herdr/projection.py:detect_workflow_stalls()` 在判定阶段推进悬挂（`stage_advance_hang`）时，代码注释写着 `# 2. Detect stage advance hang: all current tasks done/cleaned, but workflow still running`，但实际函数签名只接收 `(workflow_id, tasks)`，根本没有传入或检查 `workflow` 实体状态！只要当前传入的全部任务状态属于终态（`cleaned`/`committed` 等）且距最后完成时间超过 45 秒，函数便无条件返回 `is_stalled = True`；
2. **已交付工作流天然符合该误报条件**：任何一个正常交付关闭的工作流，其所有任务必然早已全部完成（终态）且时间已过去很久，因此系统对所有历史已完成工作流**100% 永久误报**「推进停滞」；
3. **拓扑终点（Terminal Stage）未排除**：在软件工程 6 阶段模板（`requirements -> plan -> impl -> test -> review -> wrapup`）中，当 `wrapup`（收尾）任务全部完成时，工作流已经到达拓扑终点，根本不存在所谓的“后继阶段”，告警文案与建议操作语义自相矛盾；
4. **控制台缺乏已交付态势表达**：控制台在工作流已完成（`completed`/`delivered`）时，未渲染专属的交付归档横幅，导致误报横幅独占视线；
5. **阶段状态聚合判定缺陷（`stage_summary` 中的 `committed` 陷阱）**：控制台 `stage_summary()` 之前仅在所有任务为 `cleaned` 时才判定阶段为 `cleaned`（已完成）；若任务包含 `committed`/`integrated`/`completed` 则被归类为 `finalizing`（渲染为「收尾中」并高亮显示为 active 阶段）。因代码实现任务（如 `implementation` 阶段）保留 clone 证据而天然停留在 `committed`，导致已完成工作流的阶段 3（实现）永久显示为「收尾中」，与实际交付状态完全背离。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **只查子任务状态，不查父实体生命周期** | 局部的任务完成不等于整体仍在等待推进，子实体的聚合状态必须置于父实体的生命周期上下文中评估 | 任何工作流级别的异常检测（如挂起/停滞/死锁），必须前置校验 `workflow` 状态：凡 `completed`、`closing`、`failed`、`paused` 或已标记 `outcome`（`delivered`/`abandoned`），一律快速返回非停滞 |
| **盲目假设阶段之间必有后继** | 线性推进有终点，DAG 有 Sink 节点；不是所有“当前阶段完成”都意味着“需要推进到下一阶段” | 阶段推进停滞检测必须识别拓扑终点：当全部任务已包含收尾阶段（`wrapup`）或满足 `is_workflow_completed(cfg, completed_nodes)` 时，判定流程已结束而非推进悬挂 |
| **函数契约脱节：注释承诺与入参缺失** | 注释写着“but workflow still running”，入参却只有 `tasks` 列表，开发者随手写了 `all(t in terminal)` 导致逻辑假阳性 | 函数签名必须明确暴露依赖实体 `workflow: Optional[Dict]`，并在未传时具备自愈查询能力（从持久化 store 按 ID 补全）；编写单元测试必须覆盖终态实体与拓扑终点用例 |
| **操作界面状态机与后台干预按钮不自洽** | 前端弹出了「推进停滞」并渲染了「尝试推进阶段」按钮，后端却因没有下一阶段而抛出 500/RuntimeError | 告警触发条件必须与干预动作的前置条件同构校验；对于已交付工作流，控制台应展示温和积极的「已交付」归档态势横幅，而非警报横幅 |
| **阶段完成态与子任务完成态标准割裂** | Controller 判定阶段完成使用完整的 `COMPLETED_TASK_STATUSES`（含 `committed`），控制台聚合却死卡 `cleaned` | 阶段完成态判定必须与调度内核保持一致：阶段内全部 live 任务均处于 `COMPLETED_TASK_STATUSES`（`completed` / `committed` / `integrated` / `cleanup_ready` / `cleaned`）即为已完成，严禁将保留 clone 的代码提交任务误判为「收尾中」 |

### 操作规范

1. **白盒遥测核心停滞检测完善 (`herdr/projection.py:detect_workflow_stalls`)**：
   - 增加 `workflow: Optional[Dict[str, Any]] = None` 参数，未提供时自动通过 `load_workflows_data().get("workflows", {}).get(workflow_id)` 容错检索；
   - **前置终态守卫**：若 `status in {"completed", "closing", "failed", "paused"}` 或 `outcome in {"delivered", "abandoned"}` 或 `completed_at` 存在，直接返回 `is_stalled = False`；
   - **拓扑终点守卫**：若任务集合已覆盖 `wrapup` 或通过 `is_workflow_completed(workflow_config_for(workflow_id), completed_nodes)` 判定全图节点已完成，直接返回 `is_stalled = False`；
   - 保证只有处于活跃运行态且中间阶段完成任务超过 45 秒未能拉起后继任务时，才告警 `stage_advance_hang`。
2. **控制台调用链路与已交付态势呈现 (`console/herdr_factory_console.py`)**：
   - `workflow_detail(wid)` 调用 `detect_workflow_stalls(wid, ts, workflow=w)` 传递上下文；
   - `updateAttentionHub()` 在 `w.status === 'completed' || w.outcome === 'delivered'` 时，渲染优雅温和的绿色「已交付」横幅（`🎉 工作流已顺利完成全流程闭环并交付归档`），彻底消除误报惊扰；
   - **阶段完成态对齐 (`stage_summary`)**：将 `all(s in COMPLETED_TASK_STATUSES)` 统一收敛判定为 `cleaned`（已完成），彻底解决实现阶段代码任务因处于 `committed` 导致阶段被永久误标为「收尾中」并错误高亮的问题。
3. **回归测试与分发**：
   - 在 `tests/test_projection_engine.py` 中新增已完成工作流、已交付结果、已暂停工作流、收尾阶段完成等多维用例；在 `tests/test_console_stage_summary.py` 中新增 `committed` 任务阶段完成测试；
   - 运行 `./scripts/install-herdr-console.sh` 同步到 `~/.herdr-console/` 并热重载 launchd。

### 验证命令 / 证据

```bash
# 1. 验证白盒投影与停滞检测单元测试（15 passed）
pytest tests/test_projection_engine.py -v

# 2. 验证控制台阶段聚合测试（7 passed）
pytest tests/test_console_stage_summary.py -v

# 3. 验证控制台前端语法与投影 API（95 passed）
pytest tests/test_console*.py tests/test_projection_engine.py -v

# 4. 验证全仓测试套件（522 passed，0 regression）
pytest tests/

# 5. 验证真实问题工作流当前 API 返回（所有阶段为 cleaned，is_stalled 必须为 False）
curl -s "http://127.0.0.1:8765/api/workflow?id=wf-nexusarchive-54433229-20260913-111049" | jq '.data.stages'
```

---

## 47. LaunchAgent 精简 PATH 与多版本 CLI 遮蔽：用户主目录工具链前置注入与单一事实来源规范

### 问题背景

在控制台「执行者自检」中，用户反馈 `opencode` 在终端中运行完全正常，但在控制台检测中却持续报错：
```json
> build · deepseek-v4.1-flash
Error: {
  "name": "UnknownError",
  "data": {
    "message": "Unexpected server error. Check server logs for details.",
    "ref": "err_e56cf20b"
  }
}
```
自检结果判定 `opencode` 不可用。

排查发现其根因为**环境割裂与多版本遮蔽（Shadowing）**：
1. **多版本共存与版本断代**：用户机器上存在两个 `opencode`：
   - 用户目录：`~/.opencode/bin/opencode`（v1.18.31，用户通过官方安装器更新的最新版，支持最新的模型协议，测试秒级通过）；
   - 系统目录：`/opt/homebrew/bin/opencode`（v1.18.30，Homebrew 安装残留，已过时，调用服务端触发 500 UnknownError）；
2. **环境差异与优先级倒置**：
   - 用户交互式终端（zsh）：`~/.zshrc` 将 `~/.opencode/bin` 置于 `$PATH` 最前，因此终端永远命中 v1.18.31；
   - 后台常驻 LaunchAgent（`com.user.herdr-factory-console`）：plist 声明精简 `$PATH`（`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`），不包含用户主目录；
   - `herdr/agent_binary.py` 的 `resolve_binary` 逻辑为：`shutil.which` -> `EXTRA_BIN_DIRS` -> login-shell。在 LaunchAgent 精简 PATH 下，`shutil.which` 优先命中 `/opt/homebrew/bin/opencode`（旧版），导致用户主目录的更新版被系统全局陈旧版本压制；
   - 且 `EXTRA_BIN_DIRS` 遗漏了 `~/.opencode/bin`（以及 `.cargo/bin`, `.bun/bin`, `.grok/bin`, `.kimi-code/bin` 等常见 agent 目录）。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **LaunchAgent 精简 PATH 缺少用户主目录** | 常驻守护进程不加载用户 shell RC，缺少 `~/.xxx/bin` 导致无法直接访问用户空间安装的 CLI | 系统层必须统一定义 `USER_BIN_DIRS`，在守护进程启动与模块加载时自动前置注入到 `os.environ["PATH"]` |
| **系统旧版本遮蔽用户新版本** | 在 Unix/macOS 规范中，用户主目录工具链优先级应高于系统全局目录；若直接使用精简 PATH，会导致系统遗留旧版本抢占执行权 | 二进制解析必须保证**用户主目录安装（User-space）永远优先于系统全局目录（Homebrew/System）** |
| **硬编码候选目录遗漏新异构 Agent** | 各 Agent CLI 官方安装器路径多样（如 `~/.opencode/bin`, `~/.kimi-code/bin`, `~/.grok/bin`），缺少一处就会退化为昂贵的进程 fork | 在 `herdr/agent_binary.py` 中完整收录主流 Agent 专属 bin 目录，作为全系统的单一事实来源 |

### 操作规范（已固化到 `herdr/agent_binary.py` 与 `tests/test_agent_binary_resolution.py`）

1. **统一用户目录列表与前置注入 (`ensure_user_bin_dirs`)**：
   - 定义 `USER_BIN_DIRS`（包含 `.opencode/bin`, `.local/bin`, `.volta/bin`, `.cargo/bin`, `.bun/bin`, `.grok/bin`, `.kimi-code/bin`, `.qoder-cn/entry`, `.qoder-cn/bin`, `.qoder/bin`, `.qodersec/bin`）；
   - 在模块导入时自动执行 `ensure_user_bin_dirs()`，将存在的主目录前置拼入 `os.environ["PATH"]`，消除 LaunchAgent 与终端的环境割裂；
2. **构建高优先级有序 `EXTRA_BIN_DIRS`**：
   - `EXTRA_BIN_DIRS = USER_BIN_DIRS + [/opt/homebrew/bin, /usr/local/bin]`，保证即使未命中 PATH，备选遍历也是用户主目录在前、系统目录在后；
3. **部署热加载**：
   - 运行 `./scripts/install-herdr-console.sh` 同步到 `~/.herdr-console` 并重启服务。

### 验证命令 / 证据

```bash
# 1. 验证精简 PATH 下解析优先级
env -i HOME=$HOME PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin python3 -c "from herdr import agent_binary; print(agent_binary.resolve_agent_binary('opencode'))"
# 必须输出: /Users/user/.opencode/bin/opencode

# 2. 验证二进制解析与路径注入测试套件（11 passed）
pytest tests/test_agent_binary_resolution.py -v

# 3. 验证控制台 deep-preflight 接口真实返回
curl -s "http://127.0.0.1:8765/api/deep-preflight?id=nexusarchive-54433229&agent=opencode" | jq '.data.agents[0]'
# 必须返回: final_status 为 "READY"，binary 为 "~/.opencode/bin/opencode"
```

---

## 59. 跨进程 Agent 调度中可执行文件路径解析防崩与环境自愈 (ENOENT 防御与动态兜底)

### 问题背景

在与外部 Agent OS / 协同中台（如 StaffAI / agency-agents）整合落地时，任务执行第一次尝试即报错：
`Error executing claude: spawn /Users/user/.nvm/versions/node/v22.22.2/bin/claude ENOENT`。
排查发现多重诱因：
1. **环境变量陈旧硬编码**：`.env` 中遗留了不存在的旧 Node/nvm 版本路径，适配器代码优先读取 `process.env.AGENCY_TASK_CLAUDE_PATH` 时直接使用，未校验路径真实性；
2. **路径解析器未校验可执行性**：`resolveExecutablePath(cmd)` 在遇到包含路径分隔符（`path.sep`）的输入时直接原样返回，导致无效路径直接被送入 `child_process.spawn` 触发 ENOENT；
3. **模板加载器 Python 3.14 严格类型契约**：`herdr/workflow.py` 的 `load_template(None)` 在 Python 3.14 环境下执行 `Path(None).expanduser()` 抛出 `TypeError: argument should be a str or an os.PathLike object, not 'NoneType'`；
4. **非交互 CLI 探针挂起**：Deep Preflight 探测 Claude CLI 时缺少 `--permission-mode bypassPermissions` 且子进程未显式提供空输入 EOF，导致在非 TTY 管道中阻塞。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **环境变量配置失效硬编码导致 ENOENT** | 开发者机器迁移、多版本 node 切换会导致 `.env` 中的绝对路径失效，若直接透传会导致系统停摆 | 必须在环境读取层做真实性校验，无法执行时立即降级为通用名称重新在候选链中动态寻找 |
| **路径解析器信任带斜杠的参数** | 带斜杠不代表文件可执行，直接返回坏路径违背了 Resolver 的职责 | `resolveExecutablePath` 必须先调用 `isExecutable()`，若不可行则提取 `path.basename()` 继续在全局/用户候选目录及 PATH 中深搜 |
| **模板加载缺省传参在 Python 3.14 下抛错** | 早期版本允许缺省但参数未在函数签名赋初值，上游传 None 会绕过默认参数 | 必须在函数体内显式做 `name_or_path = name_or_path or DEFAULT_TEMPLATE_NAME` 兜底防卫 |
| **非交互探针遭遇权限确认或等待输入** | 各 Agent CLI 在无 TTY 下行为各异，Claude Code 会检查权限或 stdin | 探测命令必须显式带上 `--permission-mode bypassPermissions`、`--no-session-persistence`，且 `subprocess.run` 必须显式传入空字符串以发送 EOF |

### 操作规范

1. **Resolver 防御闭环 (`executable-resolver.ts`)**：
   - 即使传入绝对路径，也先检查 `isExecutable(cmd)`；
   - 若不成立，剥离路径提取文件名 `cmd = path.basename(cmd)`，在 PATH 与预设全局目录中搜索。
2. **模板与项目创建自愈 (`workflow.py`, `projects.py`)**：
   - `load_template(name_or_path: Optional[str] = None)` 强制赋予 `DEFAULT_TEMPLATE_NAME = "software-development-v1"`；
   - `provision_project` 强制赋值 `template_name = template_name or "software-development-v1"`。
3. **精准探针参数隔离 (`deep_preflight.py`, `herdr-factory`)**：
   - 探针调用时注入 `input=""` 确保立即 EOF；
   - 对指定 Agent 的工作流启动，Deep Preflight 只探测目标 Agent，避免无关慢速 Agent 阻塞整体流程。

### 验证命令 / 关联证据

```bash
# 1. 验证 HAFlow 全量 531 项测试全部通过
pytest tests/
# 必须输出: 531 passed

# 2. 验证 StaffAI 后端适配器全量测试通过
cd /Users/user/agency-agents/hq/backend
npm run build && AGENCY_UNDER_NODE_TEST=1 AGENCY_TEST_MODE=mock node --test dist/__tests__/haflow-adapter.test.js dist/__tests__/runtime/claude-adapter.test.js
# 必须输出: 12 passed, 0 failed

# 3. 验证真实任务端到端调度成功（执行 SEO 优化任务）
curl -s -X POST http://localhost:3333/api/tasks/20260917002/execute \
  -H "Content-Type: application/json" \
  -d '{"executor":"haflow","summary":"从 StaffAI 指派 百度 SEO 专家 进行网站 SEO 诊断"}'
# 必须返回: "status":"completed", "executor":"haflow", "runtimeName":"haflow_execution_core", "degraded":false
```

---

## 60. Agent 健康快照的时效边界与"投递死亡"熔断缺失：从小时级静默空等到有界自愈

### 问题背景

2026-09-17 工作流 `wf-nexusarchive-0917-01`（nexusarchive「编辑凭证信息」）上午 11:51 启动，到晚上 18:45
仍在返工循环中，其中完全"空转"的浪费约 3.5 小时。三处独立断链在同一工作流叠加暴露：

1. **需求阶段空等 2h50m**：`requirements-challenger` 于 11:53:30 进入 `dispatched`，直到 14:44:00 才进入
   `working`——Pane 全程无投递痕迹（屏幕上没有 `HERDR_ORCH_TASK` 标记），Sentinel 的 Nudge 条件依赖
   屏幕标记存在，投递完全失败时永不触发；节点因等它 join 而整体停滞。
2. **测试阶段被派给坏 Agent**：`test-auto` 经 `--agent auto` 路由到 `pi`，而该工作流启动体检已将
   `pi: AUTH_REQUIRED` 记录在 `unhealthy_agents`；pi 3 秒即输出空 `agent_done`，总指挥两次判定回合
   超时后标记 failed，人工 28 分钟后才用 `--supersedes` 重新派发 r2（claude）。
3. **失败任务无自动恢复**：任务 failed 后节点永久空转，`herdr/direct_dispatch.py` 的补派只认
   `superseded`（无替代）的任务；`failed` 不在任何自动补派路径内，只能人工介入。

同工作流另有两处非本次修复的观察（证据留档）：`[COORDINATOR BUSY] waited=901s -> [COORDINATOR STALLED]`
显示单总指挥 LLM 串行回合仍是长尾；executor `agent_done -> completed` 间隔 73 分钟。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **健康名单只在非空时做交集** | `healthy_agents` 是"当时健康"的正向快照，冷启动（名单为空）或快照过期时，`unhealthy_agents` 里的坏 Agent 照样入选，路由黑洞让体检数据形同虚设 | 健康门禁必须**先做减法**：`unhealthy_agents` 中的 Agent 永不自动入选；正向名单只做加法（交集）且必须校验 `preflight_checked_at` 时效（默认 1800s，env `HERDR_PREFLIGHT_TTL`），过期快照降级为"仅减去 unhealthy" |
| **派发无投递 ack 熔断** | `dispatched` 只是"指令已发出"，不等于"Agent 已接单"；两次投递（send-text 落屏 + 回车 ack）任一失败都会留下永久 `dispatched` 僵尸任务 | 任何跨进程投递必须有 SLA 熔断：超时 + Pane 无投递痕迹 + Agent 非 working → 自动失败并通知（`HERDR_DISPATCH_DELIVERY_SLA`，默认 600s） |
| **Nudge 条件依赖标记存在** | 屏幕标记（`HERDR_ORCH_TASK`）是"投递成功"的证据；用它做 Nudge 前置条件，恰好把"投递完全失败"这一类最需要救援的场景排除在外 | 救援路径必须区分"有标记但 Agent 假死"（Nudge）与"无标记投递失败"（熔断失败），二者证据互补 |
| **基础设施失败混入人工恢复路径** | 投递熔断/进程崩溃属可重试的基础设施故障，与质量类失败（测试 FAIL、总指挥判 failed）完全不同，却共用"等人工 relaunch"的恢复路径 | 只有基础设施类失败（`dispatch_delivery_fuse`/`agent_process_crash`）允许自动 supersede + 补派 `-rN`；谱系（忽略 `-rN` 后缀）失败次数封顶 2 次，质量类失败绝不自动翻案 |

### 操作规范（已固化到 `herdr/agent_router.py`、`herdr/liveness.py`、`services/herdr-sentinel.py`、`services/herdr-controller.py`）

1. **健康门禁减法优先 + 快照时效 (`herdr/agent_router.py#choose_agent`)**：
   - 候选过滤链固定为 `allowed - disabled - unhealthy`，正向 `healthy` 交集仅在
     `preflight_snapshot_fresh(record)` 为真时生效；`preflight_checked_at` 缺失/不可解析视为新鲜
     （legacy 记录与单 Agent 优雅回退语义不变）；显式指定坏 Agent 仍然抛错阻断。
2. **投递熔断 (`herdr/liveness.py#evaluate_dispatch_fuse` + `services/herdr-sentinel.py#check_dispatch_fuse`)**：
   - 纯函数按 `(task_id, updated_at)` episode 只告一次；`requeues` 计数跨重派继承；
   - Sentinel 仅在"Pane 无 `HERDR_ORCH_TASK:<task_id>` 标记且 Agent 非 working"时置 failed
     （reason `dispatch_delivery_fuse`），有标记或已在 working 只通知不处置；
   - `HERDR_DISPATCH_FUSE=0` 可整体关闭。
3. **基础设施失败自动补派 (`services/herdr-controller.py#recover_infra_failed_tasks`)**：
   - registry watcher 对 failed 任务调用纯选择器
     `herdr/liveness.py#select_infra_failures_for_recovery`（节点全无活跃任务 + 基础设施原因 +
     谱系次数未达上限 + 未被 supersede），命中后 `herdr-task supersede` 并清除该节点
     stage-advance 闩，由既有 sweep 自动补派替代任务；`HERDR_AUTO_RECOVER_MAX` 调整谱系上限。
4. **门禁与回归**：上述策略均伴有负向单测（健康拓扑、过期快照、质量失败不翻案、谱系封顶、
   标记存在只告警），同类问题第 2 次出现时按知识技能决策树升级为门禁。

### 验证命令 / 关联证据

```bash
# 1. 路由健康门禁与时效（RED 先行的负向用例）
pytest tests/test_agent_router_preflight.py -q
# 期望：7 passed（含 stale snapshot 不锁死、unhealthy 冷启动不入选、显式请求坏 Agent 仍抛错）

# 2. 投递熔断 + 自动补派纯逻辑与装配
pytest tests/test_dispatch_fuse.py -q
# 期望：18 passed（DISPATCH_DELIVERY_SLA 违约、episode 去重、谱系封顶、质量失败不翻案）

# 3. 全量回归（不得有回退）
pytest -q
# 期望：578 passed, 35 subtests passed

# 4. 现场激活证据（2026-09-17 实测）
#    sentinel kickstart 后立即捕获真实僵尸任务：
#    [SENTINEL FUSE] task=wf-agency-agents-0917-05-requirements-executor waited=2064s marker=False agent=idle action=failed
#    [SENTINEL STATE] ...: dispatched -> failed (dispatch_delivery_fuse)
#    controller kickstart 后自动接住总指挥补派任务：
#    [RECOVERY] task=wf-nexusarchive-0917-01-implementation-fix-r2 registry=dispatched agent=working
```

### 相关文档 / 关联证据

- 工作流现场：`~/.herdr-controller/tasks.json`（`wf-nexusarchive-0917-01-*` 任务史）
- 日志铁证：`~/.herdr-controller/logs/controller.out.log`（`[COORDINATOR BUSY] waited=901s`、
  `[DECISION TIMEOUT]`、`[FIX LOOP NOTIFIED]`）
- 新增测试：`tests/test_agent_router_preflight.py`、`tests/test_dispatch_fuse.py`
- Wiki：[`wiki/agent-routing-and-pools.md`](../../wiki/agent-routing-and-pools.md) §4、
  [`wiki/architecture.md`](../../wiki/architecture.md) §2.1/§2.2

---

## 61. 补派集合无谱系去重导致指数放大 + agent_done 验收对单总指挥 LLM 的强依赖

### 问题背景

同日同一工作流（`wf-nexusarchive-0917-01`）在 §60 修复上线后暴露出第二组结构性浪费，
两者共同解释了 8 小时里"只有 ~3.2h 在真实干活"的账：

1. **重复补派指数放大**：fix-loop 第 2 轮触发阶段推进后，Controller 一次性派出
   `test-auto-r4`（claude）与 `test-auto-r5`（grok）两个重复测试任务，日志铁证
   `[STAGE ADVANCED DIRECT] node=test tasks=...-r4,...-r5`。根因：`plan_stage_dispatch`
   的补派集合 = 全节点 `superseded 且无 superseded_by` 的历史任务——直接派发补派时
   从不回写 `superseded_by`，于是 r2、r3 永远留在集合里；第 1 轮 awaiting={r2} 派 1 个，
   第 2 轮 awaiting={r2,r3} 派 2 个，第 3 轮就会派 4 个。单工位（`parallel: false`,
   `max_agents: 1`）test 节点因此并发双跑，且两个任务各自触发一轮总指挥验收。
2. **agent_done 验收强依赖单总指挥 LLM**：任务完成后必须等总指挥写 verdict/状态，
   而总指挥是每工作流一个 Pane 的串行 LLM。状态史聚合显示验收等待累计 **~2.6h/8h**
   （requirements executor agent_done 74min、implementation 42min、test 33min），
   且总指挥的长回合（566K tokens 上下文做深度核查）会阻塞排在后面的门禁事件投递
   （`[COORDINATOR BUSY] waited=762s+`）。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **补派集合无谱系概念** | "被作废的任务"是集合语义，但任务有替换谱系（x→x-r2→x-r3）；不按谱系去重，历史项会被反复补派，随轮次指数放大 | 补派必须按谱系取"最新一发"；谱系内只要还有非 superseded 成员（在跑/已落定）就视为已有代表，不再补派 |
| **规划器只看 awaiting 不看 active** | 单工位节点的并发红线不能只靠模板声明，规划器必须持全局视图：awaiting 与 active 在同一谱系里互斥 | 谱系判定必须同时读取同谱系全部状态；人工手动 supersede 最新一发时，同谱系在跑成员仍要能阻止再补派 |
| **非门禁节点的验收被 LLM 化** | 需求/计划/实现的验收标准是"产物落盘 + 变更受控"，`verify-baseline` 铁证已足够；让 LLM 做橡皮图章既慢又挤占门禁事件的投递通道 | 非门禁节点规则化验收（TASK_CHANGED → completed）；门禁节点（test/review/wrapup）与证据不足者保留总指挥裁决 |
| **配置异常可能误吞门禁** | 自动验收若在配置读取异常时 fail-open 会绕过门禁 | 配置不可判定时保守视为门禁（fail-closed），绝不自动翻案 |

### 操作规范（已固化到 `herdr/direct_dispatch.py`、`services/herdr-controller.py`）

1. **谱系补派去重 (`herdr/direct_dispatch.py#lineage_redispatch_candidates`)**：
   - 按 `lineage_key`（`x`→(x,1)，`x-r2`→(x,2)）分组；
   - 组内存在任一非 superseded 成员 → 跳过该谱系；
   - 全组已作废时取序号最新一发（且无 `superseded_by`）作为唯一补派对象。
2. **规则化验收 (`services/herdr-controller.py#try_auto_accept`)**：
   - 仅对 `agent_done` 的非门禁节点生效，`verify-baseline` 解析
     `HERDR_BASELINE_RESULT.changes` 非空才置 `completed`；
   - 门禁节点、`BASELINE_MATCH`、配置不可判定、`HERDR_AUTO_ACCEPT=0` 一律回落总指挥；
   - 快路径在 `_process_coordinator_item` 的任何 coordinator prompt 之前执行。
3. **回归门禁**：谱系去重与自动验收均有负向单测（活跃成员阻止补派、门禁不自动过、
   证据不足回落、环境开关），同类问题升级时优先扩这两组用例。

### 验证命令 / 关联证据

```bash
# 1. 谱系去重(含事故复现:r2/r3 双补派 → 只取最新一发)
pytest tests/test_direct_stage_dispatch.py -q
# 期望: 34 passed, 23 subtests passed

# 2. 规则化验收(门禁不自动过 / TASK_CHANGED 才完成 / 开关生效)
pytest tests/test_auto_acceptance.py -q
# 期望: 9 passed, 5 subtests passed

# 3. 全量回归
pytest -q
# 期望: 591 passed, 40 subtests passed

# 4. 现场复现(用真实 tasks.json 模拟 test 节点决策)
#    r4 working + r5 superseded 状态下:
#    mode=wait | reason=node has active tasks | specs=[]   ← 旧逻辑会再派 3 个重复任务
```

### 相关文档 / 关联证据

- 日志铁证：`[STAGE ADVANCED DIRECT] workflow=wf-nexusarchive-0917-01 node=test tasks=...r4,...r5`
- 现场处置：`herdr-task supersede wf-nexusarchive-0917-01-test-auto-r5 --reason "duplicate redispatch..."`
- 新增测试：`tests/test_auto_acceptance.py`、`tests/test_direct_stage_dispatch.py`（新增 4 例）
- Wiki：[`wiki/dag-workflow-engine.md`](../../wiki/dag-workflow-engine.md) §4.3、
  [`wiki/architecture.md`](../../wiki/architecture.md) §2.1

---

## 62. 门禁 verdict 契约化：从"LLM 转写自然语言报告"到"机器可采纳结论"

### 问题背景

延续 §60/§61 的同一工作流（`wf-nexusarchive-0917-01`）：非门禁节点验收已规则化后，
剩余长尾全部集中在门禁节点（test/review/wrapup）——门禁结论（pass/blocked）只存在于
Agent 的自然语言报告里，必须由另一个 LLM（总指挥）阅读并"转写"为
`herdr-task set <task> completed --verdict ...`。实测代价：

1. 总指挥上下文累积到 **566K tokens**（57%），单回合时长 10-20 分钟；r3 的 blocked
   判定等待、r5 的 done 事件投递等待（`[COORDINATOR BUSY] waited=900s`）都源于此；
2. 长回合还会阻塞排在后面的门禁事件投递，形成"越忙越慢"的正反馈；
3. 结论信息在转写中可能被压缩/改写（r3 的 note 与报告原文并不完全一致）。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **门禁结论无机器契约** | 结论以自然语言存在于报告里，机器无法采纳，必须再花一个 LLM 回合"转写"；这是纯粹的格式损失，不是判断损失 | 门禁结论必须契约化：固定的文件路径 + 终端标记行，写成机器可直接采纳的结论 |
| **单通道信号易被污染** | 只看文件可能读到上一轮旧文件；只看屏幕可能读到滚动残影或历史标记 | 双通道（Clone 内 JSON + 终端标记）必须**结论一致**才采纳；缺失或冲突一律回落总指挥 |
| **门禁自证的边界** | 让被测 Agent 自报结论存在自证风险 | blocked 结论必须带原因清单且直接触发回流返工（有真实代价）；fix-loop 后由新一发独立重测；总指挥仍是冲突/缺失时的裁决者与最终兜底 |

### 操作规范（已固化到 `herdr/direct_dispatch.py`、`services/herdr-controller.py`）

1. **契约注入 (`herdr/direct_dispatch.py#gate_verdict_contract`)**：
   - 仅当 `plan_stage_dispatch(..., gate_contract=True)`（Controller 由
     `node_is_gate` 判定）时，在门禁任务 prompt 末尾追加契约：
     写 clone 外状态目录 `~/.herdr-controller/gate-verdicts/<task_id>.json`
     （`{"verdict": "pass|blocked", "note": "..."}`；`HERDR_GATE_VERDICT_DIR` 可覆盖；
     权限受限时退回 `<clone>/.herdr/gate-verdict.json`）
     + 终端输出 `HERDR_GATE_VERDICT: pass|blocked`。
   - **2026-09-17 修订（lessons §64 关联）**：结论文件从 clone 内迁到状态目录——
     clone 内文件会被 `herdr-task commit` 带进交付（实测污染 nexusarchive 交付 PR）；
     同时 `bin/herdr-task` 的 `INTERNAL_UNTRACKED_*` 过滤器新增 `.herdr` / `.herdr/`，
     commit 与 verify-baseline 均不再计入该目录（兜底防线）；Controller 读取时
     状态目录优先、clone 路径兼容回退。
2. **规则化裁决 (`services/herdr-controller.py#try_auto_verdict`)**：
   - `read_gate_verdict` 合并文件与屏幕两路信号，归一化（pass/passed/ok → pass；
     blocked/block/fail/failed → blocked），仅当唯一结论才返回；
   - 采纳后调用既有 CLI 契约 `herdr-task set <task> completed --verdict ... --note ...`
     （blocked 缺 note 时自动补最小说明），随后走既有 finalize 与 fix-loop 链路；
   - 非门禁节点、信号缺失/冲突、`HERDR_AUTO_VERDICT=0` 一律回落总指挥。
3. **对存量在跑门禁任务**：可用 `herdr-task steer <task_id> "<契约补充说明>"` 在
   工位空闲时补注入契约（Sentinel 的 Steering Mesh 负责投递），无需重启任务。

### 验证命令 / 关联证据

```bash
# 1. 门禁契约与规则化裁决用例
pytest tests/test_auto_acceptance.py -q
# 期望: 19 passed, 5 subtests passed

# 2. 门禁契约注入(仅门禁节点)
pytest tests/test_direct_stage_dispatch.py -q
# 期望: 37 passed, 28 subtests passed

# 3. 全量回归
pytest -q
# 期望: 603 passed, 40 subtests passed

# 4. 现场只读校验(真实工作流)
#    review 节点 prompt 含 HERDR_GATE_VERDICT 与 .herdr/gate-verdict.json → True
#    对 r4 任务 read_gate_verdict() → (None, '', '')  ← 无标记时不误判
#    node_is_gate('wf-nexusarchive-0917-01', 'test') → True
```

### 相关文档 / 关联证据

- 现场：`herdr-task steer wf-nexusarchive-0917-01-test-auto-r4 "<契约补充>"`（存量任务补契约）
- 新增测试：`tests/test_auto_acceptance.py`（GateVerdictUnitTest 9 项 + 门禁 wiring 1 项）
- Wiki：[`wiki/dag-workflow-engine.md`](../../wiki/dag-workflow-engine.md) §10、
  [`wiki/architecture.md`](../../wiki/architecture.md) §2.1

---

## 63. commit 门禁瞬时失败（flaky gate）无重试路径：`completed` 任务死区

### 问题背景

同一工作流收尾阶段实测：`implementation-fix-r2` 在 19:11 验收通过进入 `completed`，
Controller 随即执行 `herdr-task commit` 被目标仓 bugfix 门禁拦截——
`❌ bugfix 工程化验证失败：frontend.test ok=False`（`ErrorBoundary.test.tsx` 的
"test environment was torn down" 型抖动，来自 `reports/verify/*-bugfix.json` 历史
采样：同一 profile 在 11:13/11:29/11:44 UTC 失败、11:20/12:01 UTC 通过，约 50% 抖动率）。

后果链：
1. `finalize_completed_task` 打印 `[COMMIT ERROR]` 后直接 `return`，任务停留在
   `completed`——**既不是 `committed`（有重试护栏）也不是终态**，没有任何自动重试路径；
2. 总指挥（LLM）为救场手工重试 `herdr-task commit` **5 次**，在一个 577K tokens
   的回合里空转 65 分钟（19:11 → 20:06），期间同一工作流 test 节点的
   `done` 事件一直排队（`[COORDINATOR BUSY]`），门禁验收被连带阻塞；
3. 期间正是人工重试的第 5 次运气好撞上抖动通过（`c30d99e2`），Controller 的
   `committed -> retry finalize` 护栏才接管完成 integrate + cleanup。

叠加环境因素：当时系统 load average 61/150/176（大量他项目长驻进程），
重门禁（架构检查 6687 依赖 + 全量前端测试）在过载下抖动概率显著升高，
且门禁输出中"❌/失败 profile"位于尾部，`[COMMIT ERROR]` 只回显首行，
人工排障需要翻整段日志。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **`completed` + git 是重试死区** | 中间态的"可重试"护栏必须覆盖全链路：`committed` 有护栏而 `completed`（commit 未完成）没有，等价于把最常见的第一步失败排除在自愈之外 | `committed`/`completed` 且 `integration_mode=git` 的任务统一走终化重试：退避 + 上限 + 耗尽告警 |
| **瞬时失败不可自动重试=把抖动放大成人工阻塞** | 50% 抖动率的重门禁在没有自动重试时，只能靠人/总指挥赌运气并烧掉大回合 | 对幂等的终化步骤（commit/integrate）必须自动重试，且重试必须是有界的（上限 + 指数退避 + 耗尽升级人工） |
| **门禁失败信息定位成本高** | 失败结论在门禁输出的尾部，调用方只回显首行，排障需翻整段日志 | 重门禁失败时必须把"失败 profile / 报告路径 / 查看命令"提炼为独立摘要行（目标仓门禁已输出，HAFlow 侧按需回显摘要） |
| **环境过载是门禁抖动的放大器** | load 150+ 时架构检查会出现瞬时竞态（复跑 exit=0），全量前端测试超时窗口被挤压 | 长驻重进程（旧 server / 旧 Agent 会话 / 构建残留）必须定期巡检清理；排障时先看 load 与 top 进程 |

### 操作规范（已固化到 `services/herdr-controller.py`）

1. **统一终化重试 (`should_retry_finalize`)**：
   - `status in (committed, completed)` 且 `integration_mode=git` 且工作流未关闭；
   - episode 退避窗口外才重试（`attention_blocks_retry`），reason 区分
     `integration_retry`（committed）/ `commit_retry`（completed）；
   - 尝试次数 `HERDR_FINALIZE_RETRY_MAX`（默认 5）封顶，耗尽后打印
     `[FINALIZE RETRY EXHAUSTED]` 并升级人工（只告警一次，不刷屏）。
2. **重试仍复用既有幂等链路**：`finalize_completed_task`（commit → rebase → integrate →
   cleanup）本身幂等，重试不引入新状态。
3. **排障顺序**：`[COMMIT ERROR]` → 门禁输出尾部摘要（❌ / 失败 profile / 报告路径）
   → `uptime` 与 `ps -Ao pid,%cpu,etime,comm -r | head` 排查系统过载。

### 验证命令 / 关联证据

```bash
# 1. 终化重试决策(completed/committed/退避/上限/非 git 不重试)
pytest tests/test_auto_acceptance.py -q
# 期望: 24 passed, 9 subtests passed

# 2. 全量回归
pytest -q
# 期望: 608 passed, 40 subtests passed

# 3. 现场证据
#    门禁抖动采样: reports/verify/*-bugfix.json（11:13/11:29/11:44 UTC 失败, 11:20/12:01 UTC 通过）
#    人工重试上下文: 总指挥 pane（577.9K tokens, 第 5 次提交）
#    恢复链: c30d99e2 提交 → Controller [REGISTRY WATCHER] committed -> retry finalize → cleaned
```

### 相关文档 / 关联证据

- 现场：`~/.herdr-controller/clones/wf-nexusarchive-0917-01-implementation-fix-r2/`
  （`reports/verify/*.json` 抖动采样、`reports/verify/logs/`）
- 新增测试：`tests/test_auto_acceptance.py#FinalizeRetryDecisionTest`
- Wiki：[`wiki/architecture.md`](../../wiki/architecture.md) §2.1

---

## 64. 工作流收官缺"交付 PR"环节：six-step-finish 只核验不合入，全链路无 push/PR

### 问题背景

`wf-nexusarchive-0917-01` 于 2026-09-17 20:35 完成（`[WORKFLOW CLOSED]`、outcome=delivered），
wrapup（7收尾）节点严格按模板规则执行：六步收尾步骤 1-2 完成、步骤 3 只读合并确认返回
⛔（集成分支未合入 dev）→ 记录 DEFERRED 并产出合并指引。

但事后核对发现**交付 PR 从未被创建**：

1. 集成分支 `herdr/integration-wf-nexusarchive-0917-01-implementation-fix-r2` @ `c30d99e2`
   **只存在于目标仓本地**——`git ls-remote origin 'refs/heads/herdr/*'` 为空；
2. `bin/herdr-task integrate` 仅在本地建集成分支 + herdr refs，全仓库
   （`bin/` / `services/` / `herdr/`）**零处 `git push`**；
3. 最终由人工要求总指挥（coordinator Pane，646K tokens 的长回合）手工补交 PR，
   而目标仓 nexusarchive 的 AGENTS.md 本就写明标准流程：`npm run pr:wrap-up`
   （= `scripts/gitee-pr.sh wrap-up`：push + 建 PR + 自动合并）。

三层根因：
- **模板断档**：wrapup 规则只要求 six-step-finish，并把"未合入 → DEFERRED"写死，
  没有"创建 PR"这一步；RULES.md S7（推送专属隔离分支 + 创建标准化 PR）无节点承接；
- **技能边界被误读**：six-step-finish 的定位是"已合入的核验与善后清理，不负责合入动作本身"，
  其文档写"未推送、未提 PR、未合入的分支会被拒绝清理……先走项目的 PR 流程"——
  但**流程里并不存在"项目的 PR 流程"这一步**；
- **本地集成分支被当成交付**：没有 push，远端无从感知，PR/评审/合并的反馈循环全部断裂。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **"交付"缺少 owner** | 规范里的交付动作（S7：推送分支 + 创建 PR）必须有明确的执行节点/步骤，否则会被技能边界悄悄漏掉 | 模板 wrapup 规则显式加入"交付 PR 前置（必做）"，位于知识沉淀之后、合并确认之前 |
| **技能文档的隐含假设** | 技能说"先走项目的 PR 流程"时，必须回到流程定义里确认"那一步真的存在" | 引入外部技能时做一次"前置条件存在性"核对：技能要求的前置步骤必须在模板/脚本中有对应实现 |
| **本地分支 ≠ 交付** | 无 push 的交付对远端不可见，评审/合并/反馈循环全部断裂 | 交付完成的判据 = 远端存在分支 + PR URL 已产出并写入收尾报告 |
| **创建与合并是两种授权** | PR 创建是交付动作，合并是授权动作；自动化不应越权合入主干 | 默认只创建 PR，严禁自动合并；合并由作者/评审决定（目标仓 SOP 明确声明自动闭环时才可另行授权） |

### 操作规范（已固化到 `workflow_templates/software-development-v1.yaml` 与 `.agents/skills/six-step-finish/SKILL.md`）

1. **模板：交付 PR 前置（必做，wrapup 规则）**：
   - 顺序：六步步骤 1-2（知识沉淀/wiki 回填并提交）→ **交付 PR** → 步骤 3 合并确认；
   - 交付 PR 三步：读目标仓交付约定（AGENTS.md / CLAUDE.md / docs/guides/*wrap-up*.md /
     package.json scripts）→ 按标准流程推送交付分支并创建 PR（如 `npm run pr:create`；
     无脚本时用 forge CLI/API）→ PR URL 与目标 base 写入收尾报告；
   - 硬约束：只允许"推送交付分支 + 创建 PR"两类非破坏性远端动作；严禁自动合并；
     严禁 `--force` / `--yes`；不得改写交付分支历史；PR 无法创建时才记 DEFERRED 并说明原因；
   - 未合入分支的 DEFERRED 记录必须携带 PR URL。
2. **技能：six-step-finish 增加步骤 0（交付 PR 前置）**：
   - 六步总览表新增"步骤 0 交付 PR（前置）"行；Agent 职责新增"先建 PR 再做核验"；
   - 常见借口表新增三条：未推送/无 PR 先跑脚本、顺手帮忙 merge、脚本代劳 PR。
3. **同步与登记**：技能为仓库单一事实源，修订后运行 `scripts/install-herdr-skills.sh`
   同步到 `~/.agents/skills/`，并更新 `PROVENANCE.md`（sha256 + 本地修订记录）与
   `tests/test_six_step_skill_provenance.py` 的登记哈希。

### 验证命令 / 关联证据

```bash
# 1. 模板交付 PR 契约（含严禁自动合并/PR URL 要求）
pytest tests/test_software_development_v1_template.py -q
# 期望: 9 passed（新增 test_wrapup_requires_delivery_pr_before_finish）

# 2. 技能 vendoring 完整性（哈希 = 修订后登记值）
pytest tests/test_six_step_skill_provenance.py -q
# 期望: 6 passed

# 3. 全量回归
pytest -q
# 期望: 611 passed, 44 subtests passed

# 4. 全局技能同步实证
rg -n "步骤 0" ~/.agents/skills/six-step-finish/SKILL.md
# 期望: 命中「前置条件（步骤 0：交付 PR）」与「先建 PR 再做核验（步骤 0）」
```

### 相关文档 / 关联证据

- 现场报告：`clones/wf-nexusarchive-0917-01-wrapup-auto/wrapup-report/SIX-STEP-FINISH.md`
  （步骤 3 ⛔ 原始输出与独立复核）
- 远端实证：`git ls-remote origin 'refs/heads/herdr/*'`（事故当时为空）
- 目标仓 SOP：`/Users/user/nexusarchive/AGENTS.md`（`npm run pr:wrap-up`）+
  `scripts/gitee-pr.sh`（create / merge / wrap-up 语义）
- 模板与技能：`workflow_templates/software-development-v1.yaml#wrapup.rules`、
  `.agents/skills/six-step-finish/SKILL.md`（步骤 0）

---

## 65. 自动 close 与 git 终化抢跑：收官"最后一公里"的交付分支丢失

### 问题背景

2026-09-17 `wf-nexusarchive-0917-01` 收官时刻（20:35-20:38）实测三方竞态：

1. wrapup 任务 20:35:48 被总指挥判 `completed`；
2. `maybe_close_completed_workflow` 因"全节点完成"立刻在**后台线程**执行
   `herdr-task close-workflow`，其中 `_finalize_one` 依据 `FINALIZE_ADVANCE`
   把 `completed` 任务直接推进 `cleanup_ready -> cleaned`；
3. **与此同时**主线程的 `finalize_completed_task` 正在运行 `herdr-task commit`
   子进程（目标仓重门禁约 2m50s）。git commit 已成功（`4872b1a0`），但随后
   的状态落盘撞 `Illegal transition: cleaned -> committed`——`[COMMIT ERROR]`
   退出，交付分支生成却未进集成链路；这单 commit 还把 `.herdr/gate-verdict.json`
   带入交付（§62 修订与目标仓 `6c9e48cf` 后来兜底清洗）。

即"全节点完成"（DAG 语义）与"交付终化收敛"（commit → integrate 语义）之间
缺少互斥：物理收尾可以在 git 终化在途时抢跑。本次运行 8h47m 里约 3.2h 真实
干活，其余为 §60（2h50m 投递静默 + 38min 坏 Agent 误派）、§61（24min 补派
放大 + 2.35h 总指挥串行）、§63（55min commit 死区）类浪费；本条是这些修复
之外**新暴露的闭环末端缺口**。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **"完成"与"可收尾"被当同一时刻** | 节点全完成只说明验收过了；git 任务还要 commit→integrate 才算交付闭环。把两者混同时刻，等于允许收尾线程与终化子进程赛跑 | 自动 close 必须等 git 终化收敛：存在 `completed`/`committed` + `integration_mode=git` 的任务时推迟（`[CLOSE DEFERRED]`），终化有界重试自会收敛 |
| **物理收尾改写 git 语义** | `_finalize_one` 对 `completed` 直接推进 `cleaned`，对 git 任务等于**静默跳过 commit/integrate**；`committed` 则被留下孤儿状态 | CLI `close-workflow` 必须显式闸门：unsettled-git 任务 `[CLOSE ABORT]`（exit 2）并给出手工收口指引；`completed+none`/`cleaned+git` 不误伤 |
| **竞态缺乏可见性** | 三方并发只留下一条 `Illegal transition` 且埋在大段门禁输出尾部，排障需翻全量日志 | 推迟 close 打印一次 `[CLOSE DEFERRED]`（防刷屏闩），现场一眼可辨；CLI abort 提示 commit/integrate/supersede 三条处置路径 |

### 操作规范（已固化到 `services/herdr-controller.py`、`bin/herdr-task`）

1. **Controller 推迟 (`git_finalize_pending_tasks` + `maybe_close_completed_workflow`)**：
   - close 前查询同 workflow 的 `completed`/`committed` + git 任务；命中则打印一次
     `[CLOSE DEFERRED] workflow=... waiting git finalize: ...` 并跳过本轮；
   - sweep 幂等重试：终化收敛（任务进入 `cleaned`）后下一轮自然放行；
   - 终化重试耗尽时任务留在 `completed`，close 持续推迟并已有
     `[FINALIZE RETRY EXHAUSTED]` 升级人工——宁可工作流不自动关闭，也不静默丢交付。
2. **CLI 闸门 (`close_workflow`)**：
   - 在 `TEARDOWN_BLOCKING_STATUSES` 检查之后追加 unsettled-git 检查，
     `[CLOSE ABORT]`（exit 2）并打印处置指引：
     `herdr-task commit <task_id>`（completed）/ `herdr-task integrate <task_id>`（committed）/
     `herdr-task supersede <task_id> --reason ...`（确要丢弃交付物）；
   - `dry_run` 同样走闸门；`integration_mode` 缺失（legacy）视为 `none`，行为不变。
3. **回归门禁**：Controller 侧 4 例 + CLI 侧 2 例负向/正向用例，同类问题复发时优先扩这两组。

### 验证命令 / 关联证据

```bash
# 1. Controller 推迟语义(completed/committed 推迟、settled/none 放行)
pytest tests/test_fix_loop_gates.py::AutoCloseGitFinalizeDeferralTest -q
# 期望: 4 passed

# 2. CLI 闸门(未收敛阻断不触达 teardown、非 git/settled 不误伤)
pytest tests/test_workflow_finalize.py -q -k "unsettled or ignores"
# 期望: 2 passed

# 3. 全量回归(不得有回退)
pytest -q
# 期望: 620 passed, 44 subtests passed

# 4. 现场铁证
#    ~/.herdr-controller/logs/controller.out.log:
#    [FINALIZE] task=...wrapup-auto integration_mode=git
#    ... [agent/claude/docs-...wrapup-auto 4872b1a0] task: ...wrapup-auto
#    Illegal transition: wf-nexusarchive-0917-01-wrapup-auto: cleaned -> committed
#    [COMMIT ERROR] task=...wrapup-auto: → Checking Node version sources consistency...
#    （同段含 create mode 100644 .herdr/gate-verdict.json — §62 关联污染）
```

### 相关文档 / 关联证据

- 现场：`~/.herdr-controller/logs/controller.out.log`（20:38 收官段）、
  `clones/wf-nexusarchive-0917-01-wrapup-auto`（`895de7d3` 制品、交付 PR `!1354`）
- 新增测试：`tests/test_fix_loop_gates.py#AutoCloseGitFinalizeDeferralTest`、
  `tests/test_workflow_finalize.py#TestCloseWorkflow`
- Wiki：[`wiki/architecture.md`](../../wiki/architecture.md) §2.1、
  [`wiki/dag-workflow-engine.md`](../../wiki/dag-workflow-engine.md) §12

---


## 66. 总指挥回合成本治理：上下文卫生 + 效率纪律（684K 上下文 58 分钟回合的账本）

### 问题背景

2026-09-17 对 `wf-nexusarchive-0917-01` 总指挥会话做了全量账本还原
（opencode session db 可按 prompt 切回合、按 message tokens 取上下文）：

1. 会话跨度 7.91h，共收到 30 个 prompt（23 个 Controller 事件 + 7 个人工/控制台指令）；
   总指挥活跃（LLM + 工具）**3.36h**，按 prompt 分组的回合累计 6.31h（含事件空窗）；
2. 上下文**单调增长 94K → 684K**，全程无压缩机制；
3. 后期回合耗时几乎全由 LLM 生成构成：**584K 上下文时单回合 LLM 生成 58.3min**、
   520K 时 23.1min、684K 时 20.4min；而早期（<200K）回合普遍 0.2-4min；
4. 单回合工具调用最多 55 次；最慢工具是**重复运行 `herdr-task commit` 重门禁**
   （14.6/13.0/6.8/2.4min）与自写轮询脚本（10.2/9.2min）——即总指挥在替 Controller
   做终化工作（当时终化重试护栏尚未上线，属人工救场，但仍暴露职责越界）；
5. 控制面后果：该工作流所有事件串行等待总指挥（`[COORDINATOR BUSY]` 187 次、
   累计 2.35h、最长 901s）——**不是全局吞吐问题，而是单工作流尾延迟被上下文拖爆**。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **单会话上下文只增不减** | LLM 控制面的回合成本是上下文规模的函数；不做治理，长工作流必然尾部爆炸（越忙越慢的正反馈） | 在**阶段边界与 fix-loop 边界**对总指挥注入 `/compact`（上下文卫生），把后续回合耗时拉回分钟级；对 opencode / claude 均有效，env 可关 |
| **控制面职责无硬边界** | 事件模板只写"要做什么"，没有"不许做什么"；LLM 会自然膨胀到运行重门禁、做人工救场 | 所有事件模板追加**效率纪律**硬约束：决策落盘即结束回合；禁止 commit/integrate/cleanup/全量测试（Controller 已自动化）；只读核验优先 |
| **回合成本缺可观测口径** | 没有量化就看不见控制面开销，优化无从下手 | 复盘口径：opencode session db 按 prompt 切回合 + message tokens 取上下文；运行时看 `[COORDINATOR COMPACT]` 与 `[COORDINATOR BUSY]` 累计值 |

### 操作规范（已固化到 `services/herdr-controller.py`）

1. **上下文卫生 (`maybe_compact_coordinator`)**：
   - 触发点：直接派发阶段推进（`[STAGE ADVANCED DIRECT]`）、总指挥阶段推进
     （`[STAGE ADVANCED]`）、fix-loop 派发（`[FIX LOOP NOTIFIED]`）三处边界；
   - 安全门：`HERDR_COORDINATOR_COMPACT=0` 整体关闭；仅对 `opencode`/`claude`
     kind 注入（`herdr agent get -> result.agent.agent` 探测）；总指挥非
     `idle`/`done` 时跳过（下个边界再补）；`--wait --timeout 300000` 有界，
     失败只记日志、绝不阻断阶段推进；
   - 日志契约：`[COORDINATOR COMPACT]` / `[COORDINATOR COMPACT SKIP]` /
     `[COORDINATOR COMPACT ERROR]`。
2. **效率纪律 (`COORDINATOR_DISCIPLINE`)**：注入 done / blocked / attention /
   retry / fix-loop / stage-advance 全部事件模板（落盘即停、禁止重操作、
   只读核验优先、上下文过大先 `/compact`）。
3. **回归门禁**：纪律注入 2 组用例 + compact 5 例（env 关闭 / kind 不支持 /
   忙跳过 / 成功注入 / 失败不阻断）+ 边界触发契约 1 例；同类问题复发时优先扩这组。

### 验证命令 / 关联证据

```bash
# 1. compact 安全门与注入契约
pytest tests/test_liveness_guard.py::CoordinatorCompactTest -q
# 期望: 5 passed

# 2. 边界触发 + 纪律注入
pytest tests/test_direct_stage_dispatch.py -q -k "compaction"
pytest tests/test_fix_loop_gates.py -q -k "discipline"
pytest tests/test_liveness_guard.py -q -k "efficiency"

# 3. 全量回归(不得有回退)
pytest -q
# 期望: 628 passed, 44 subtests passed

# 4. 账本口径(复盘用,只读)
#    session ses_f5242020dffehrpK6C4vhIpazg @ ~/.local/share/opencode/opencode.db
#    30 prompts / 上下文 94K->684K / 单回合 LLM 最长 58.3min / 回合累计 6.31h
```

### 相关文档 / 关联证据

- Wiki：[`wiki/architecture.md`](../../wiki/architecture.md) §2.1
- 新增测试：`tests/test_liveness_guard.py#CoordinatorCompactTest`、
  `tests/test_direct_stage_dispatch.py#test_direct_advance_triggers_coordinator_compaction`、
  `tests/test_fix_loop_gates.py#test_contains_efficiency_discipline`
- 现场日志契约：controller.out.log 的 `[COORDINATOR COMPACT]` / `[COORDINATOR BUSY]`

---

## 67. 工作流启动路径的两处断裂：模板选择丢失与总指挥缺席

### 问题背景

2026-09-18 用户反馈 `wf-nexusarchive-0918-01` 两个问题：

1. 控制台选择 **general-task-v1**（通用数字化任务协同流，3 节点），实际按
   **software-development-v1**（6 阶段）运行；
2. 任务启动没有经过总指挥，直接进入"需求分析"。

代码级根因：

- **模板被静默忽略**：`herdr/projects.py#ensure_project` 对已注册项目直接
  `return record`，完全忽略传入的 `template_name`。项目 workflow.json 生成于
  06:37（上一次运行），06:55 新工作流沿用旧模板；CLI `run --template` 默认写死
  `software-development-v1` 进一步掩盖了问题。
- **接单职责缺失**：启动路径自 #35 Direct Stage Dispatch 起首节点也走直派，
  总指挥只在任务事件（done/blocked/fix-loop）参与，"总指挥接单"这一产品职责
  在启动阶段不存在。

现场处置与验证：终止错模板工作流（`close-workflow --abandon`）后以修复版重启
`wf-nexusarchive-0918-02`——general-task-v1 生效（3 节点新 Tab、协调者 Pane 保留），
`[COORDINATOR INTAKE]` 命中，总指挥收到 `HERDR_WORKFLOW_INTAKE_EVENT` 并自行派发
首个任务，随后 `[COORDINATOR COMPACT]` 成功注入。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **参数默认值掩盖显式入参** | "已有则返回"的幂等路径把用户显式选择当成了可忽略的默认值，静默产生错误行为 | 生命周期函数必须区分"显式请求"与"未指定"（None 语义）；显式指定必须生效，冲突时明确拒绝而不是静默沿用 |
| **快路径吞掉产品角色** | 调度优化（直派）让首节点绕过总指挥，"接单"职责随之消失 | 产品角色必须显式建模为可开关路径（首节点路由协调者），而不是优化副作用；提供 env 退回开关 |
| **模板与运行时拓扑是一体的** | 只改 workflow.json 的模板名不够，Tab/Anchor 拓扑必须同步重编，否则运行时错位 | 模板切换 = 保留 workspace/协调者 + 重建节点 Tab/Anchor + 关闭旧节点 Tab；有活跃工作流时拒绝切换 |

### 操作规范（已固化到 `herdr/projects.py`、`bin/herdr-factory`、`services/herdr-controller.py`）

1. **模板切换 (`reprovision_project_template`)**：
   - 触发：`ensure_project`/`create_project` 收到显式模板且与当前
     `workflow.json#workflow_template` 不同；
   - 守卫：存在活跃工作流 → `RuntimeError`（明确拒绝）；workspace 不存活 →
     回到全量 `provision_project`；
   - 拓扑：逐节点创建 Tab + Anchor，关闭旧模板节点 Tab（协调者 Tab 永不关闭），
     经 `_register_project_workflow` 写回；
   - CLI 语义：`run --template` 缺省 `None`（不指定=沿用现有），控制台显式选择
     必然生效。
2. **总指挥接单 (`coordinator_intake_enabled` + `item.intake`)**：
   - 首个节点（`stage in (None, "", "start")`）且 `HERDR_COORDINATOR_INTAKE != 0`
     → 跳过直派，走协调者路径；
   - 消息头 `HERDR_WORKFLOW_INTAKE_EVENT` + 接单说明（先完整阅读需求，再按节点
     职责创建第一个 Task）；
   - 协调者不可用：沿用 stage_advance 的有界等待 + attention 慢速重试，不空转。
3. **`/compact` 观测分类**：`agent_prompt_stalled`（空会话无可压缩）归为良性
   `[COORDINATOR COMPACT SKIP] no_activity`，不再报 ERROR。
4. **前端可见性（console）**：`workflow_detail` 的阶段卡片必须按
   `workflow.json#nodes`（id/label）渲染，回退内置 `STAGES`——否则切换模板后
   运行时已变、界面仍显示旧模板阶段（实测 general-task 运行中前端仍 6 阶段）。

### 验证命令 / 关联证据

```bash
# 1. 模板切换(保留协调者/重建节点拓扑/活跃工作流拒绝)
pytest tests/test_console_project_creation.py::TemplateSwitchTest -q   # 5 passed

# 2. 接单路由(首节点走协调者 / 非首节点直派 / env 关闭)
pytest tests/test_direct_stage_dispatch.py::CoordinatorIntakeTest -q   # 4 passed

# 3. compact 空会话良性分类
pytest tests/test_liveness_guard.py::CoordinatorCompactTest -q         # 6 passed

# 4. 全量回归(不得有回退)
pytest -q
# 期望: 637 passed, 44 subtests passed

# 5. 现场实证(wf-nexusarchive-0918-02)
#    [COORDINATOR INTAKE] workflow=wf-nexusarchive-0918-02 node=intake_and_scoping
#    [STAGE ADVANCED] start -> intake_and_scoping
#    [COORDINATOR COMPACT] pane=wN:p1 kind=opencode reason=stage_advance:intake_and_scoping
#    workflow.json: workflow_template=general-task-v1, nodes=3, coordinator=wN:p1 保留
```

### 相关文档 / 关联证据

- Wiki：[`wiki/architecture.md`](../../wiki/architecture.md) §2.1
- 新增测试：`tests/test_console_project_creation.py#TemplateSwitchTest`、
  `tests/test_direct_stage_dispatch.py#CoordinatorIntakeTest`
- 事故工作流：`wf-nexusarchive-0918-01`（错模板，已 abandon）→
  `wf-nexusarchive-0918-02`（修复后重启，现场验证）

---

## 68. WIP stash 净增量判定：`git stash show` 会漏 staged 内容，反向 apply 不可作"已包含"判据

### 问题背景

2026-09-18 处理 `wip/phase0-inner-loop-arbitration` 存档时踩到两个判定陷阱：

1. `git stash show --stat "stash@{1}"` 只显示 **1 个文件**（services/herdr-controller.py，85 行），据此
   判断 WIP 净增量只有一处；用 `git stash branch` 恢复后，三点 diff
   （`git diff --stat <base>...<branch>`）实际显示 **9 文件 / 787 行**——stash 还包含
   staged 部分（Phase 0 的测试与配套实现），`git stash show` 默认并未完整呈现；
2. 反向 apply 检查（`git stash show -p | git apply -R --check`）因目标文件在两天内大量演化
   而失败，这种失败**只说明上下文不匹配**，不能作为"内容尚未合入 main"的判据
   （同理也不能用它的"成功"来证明已包含）。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| **`git stash show` 呈现不完整** | 它默认只呈现工作区部分，对 staged 内容可能缺失，会系统性低估 WIP 范围 | 判定 stash 净增量必须基于完整 tree：先 `git stash branch <archive>` 恢复为分支，再用 `git diff --stat <base>...<archive>`（三点）计算；或在不动工作区时用 `git diff stash@{n}^ stash@{n}` |
| **反向 apply 判"已包含"不可靠** | 文件演化后上下文不再匹配，反向检查必然失败，与内容是否已被覆盖无关 | 用内容级判定：抽取新增行做存在性匹配（可脚本化），或逐文件与当前 HEAD diff |
| **`git add -A` 带入运行产物** | 老的基座分支 `.gitignore` 可能未覆盖新出现的运行目录（本次 `.codegraph/`） | 存档提交前 `git status` 复核；误入的产物用独立提交移除，不 amend 已推送历史 |

### 操作规范

1. 处理 WIP stash：先恢复为**存档分支**（`git stash branch`），不在主工作区落地；
2. 用三点 diff 计算净增量：为空 → 可安全 drop；非空 → 保留分支并按净增量评估补齐；
3. 存档/提交前复核 `git status`，排除误入的运行产物；需要移除时追加一个独立提交。

### 验证命令 / 关联证据

```bash
# 本次实证:两套口径的差距
git stash show --stat "stash@{n}"                     # 1 file, 88 lines  ← 不完整
git diff --stat 974064f...wip/phase0-inner-loop-arbitration  # 9 files, 787 lines ← 完整净增量
```

---

## 69. 跨 Clone 证据链断裂：代码物理隔离下的"受控文档共享区"设计

### 问题背景

2026-09-18 评估 `software-development-v1` 与 `/unified-dev-flow` 结合方案时发现：
每个 Task 在独立 CoW clone 中运行（"生而隔离，死而清零"），而 unified-dev-flow 假设
S0–S8 是一条连续工作区证据链——requirements 节点写在 clone 内的 Entry Gate 规格、
test/review 节点的验证与评审证据，下一节点根本看不到；wrapup 也看不到 review clone
的结论。门禁 verdict 之所以落在 `~/.herdr-controller/gate-verdicts/`（clone 外），
正是对这一问题的局部规避，但文档与语义证据一直没有通道；同时治理原则写死
"跨任务唯一合法信息通道是固化产物"，使"受控共享"成为空白。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 隔离 clone 间文档/证据不可见 | 代码隔离 ≠ 上下文必须隔离；缺少共享通道会把跨阶段证据链撕成孤岛，逼下游重推或凭记忆 | 跨任务信息通道 = 固化产物 + **受控共享文档区**；共享区必须 clone 外、append-only |
| 自由共享盘会引入写冲突与幻觉注入 | 并发 Pane 裸写共享目录 → 文档冲突替代代码冲突；未验证的 prose 被下游当事实 | CLI 中介写入 + 单写者纪律 + 权威层级（`git/verify-baseline > controller 机器证据 > 文档`） |
| 证据失效无标记 | 源码变化 / fix-loop 后旧证据仍可被引用，违反"改动即失效"不变量 | 记录 `base_sha`；`evidence/gate` 类在 base 漂移时读取即标 STALE；fix-loop 写 invalidation 记录作废早于作废点的目标节点条目 |
| 共享状态无生命周期终点 | 易膨胀为"第四份状态"并污染交付 diff | 物理位置在 clone 外；随 workflow 归档保留供审计，不自动删除 |

### 操作规范（已固化到 `herdr/workflow_docs.py` / `bin/herdr-task` / `services/herdr-controller.py` / `wiki/task-lifecycle.md §5`）

1. 新增跨任务上下文一律走 `herdr-task note-add`（append-only，自动带 node/task/agent/base_sha provenance），禁止裸写共享目录；
2. 下游节点与门禁不得把共享文档当事实，只作上下文；机器证据由 controller/门禁 verdict 自动落盘（`kind=gate` / `kind=invalidation`）；
3. 源码任何相交变更 / fix-loop 后，依赖旧证据的结论必须重跑——STALE 标记只提示，不替代重新验证；
4. 共享区严禁进入 `verify-baseline` 交付 diff；交付文档（`docs/`、`wiki/`）仍走任务分支提交。

### 验证命令 / 守护测试

```bash
pytest tests/test_workflow_docs.py tests/test_workflow_docs_cli.py tests/test_direct_stage_dispatch.py -q
# 期望输出：76 passed, 23 subtests passed（其中共享文档区新增 29 例）

pytest -q
# 期望输出：677 passed, 44 subtests passed
```

### 相关文档 / 关联证据

- `herdr/workflow_docs.py` — 账本 / stale / 摘要渲染核心
- `bin/herdr-task` #note_add / #note_list / #_record_gate_note
- `services/herdr-controller.py` #shared_docs_block / #_record_invalidation_note
- `wiki/task-lifecycle.md §5`、`wiki/log.md [2026-09-18]`
- 关联教训 §44（多会话并行必须 CoW 沙盒隔离）——本条为其对偶：隔离之上补受控共享通道
- 分支 `feat/workflow-shared-docs`（PR 编号见 PR 描述）

---

---

## 70. 旁路决策层接管既有控制流：interception/handled 语义、durable 拦截与动态 Kill Switch

### 问题背景

2026-09-18 Semantic Supervisor V1（PR #60）评审发现三处 P1，均属"旁路观察层"被低估为纯观察所致：

1. **控制流冲突**：`supervisor_checkpoint()` 返回后 Controller 仍无条件 `enqueue_coordinator_event("done")`。一旦 `enforce=true`，Policy 的 RETRY/ESCALATE/VERIFY/PAUSE/REROUTE 与原 done 流程正面冲突——RETRY 已把任务置 `rework`，done 事件仍会发出，状态机与总指挥验收全部错位。
2. **语义判断无证据**：`SupervisorState` 声称支持 `tests/diff_summary/output_summary`，但 harness 从未采集；9 个语义信号只能靠 goal/事件"凭感觉"判断，meaningful_progress / tests_sufficient 等没有可追溯事实源。
3. **Kill Switch 形同虚设**：`HERDR_SUPERVISOR_JEV_ENABLED=false` 无人检查；provider 构造时缓存 `JEV_API_KEY`，运行中的 Controller 撤销 key 后仍继续发请求。

根因：旁路层一旦拥有 enforce 能力，就必须重新定义"它与主控制流的边界"，而不仅是"多返回一个判断"。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 旁路返回后主流程照旧执行 | 可干预的旁路层必须显式返回拦截语义，主流程的默认出口必须有且只有一个网关 | `run_checkpoint` 返回 `{intercepted, handled, continue_flow}`；默认 done 出口全部收敛到唯一网关；**未映射/崩溃同样拦截**，绝不静默放行 |
| 成本闸门（RateGate）跳过评估 = 拦截失效 | 拦截不能依赖"每次重新评估"；补投、恢复、watchdog 路径同样会被跳过穿透，必须落在 durable 账本上 | 从 events 账本读最近一次 `enforced` intervention 作为待决标记；新 agent_done 变迁或新决策到来才解除；observe 模式与关闭监督时必须完全恢复原行为 |
| "支持某字段"≠"采集了该字段" | 语义判断的质量上限由证据采集决定；State 声明字段而无采集实现 = 隐性幻觉入口 | 证据采集与 State 组装分层（`evidence.py`）；只发送有界摘要，禁止完整源码/diff/stdout；每个事实字段必须有采集实现 + 预算/脱敏测试 |
| Kill Switch 只在单层检查 | 环境开关与凭据必须每次请求动态解析、多层短路；构造时缓存 secret 即等于开关失效 | provider 不缓存 secret；`*_ENABLED=false` 在 harness/engine/provider 三层短路可验证零请求；key 不落 config/event/log |

### 操作规范（已固化到 `herdr/supervisor/`、`herdr/decision/providers/jev.py`、`services/herdr-controller.py`）

1. 新增可干预旁路层时，先定义动作集合与 pass-through/intervention 边界，再让所有默认出口经过唯一网关（HAFlow 为 `emit_done_if_allowed()`）；
2. 拦截三件套必须有回归断言：handler 成功 / 未映射 / handler 崩溃三种情形都不得继续默认流；
3. 成本闸门不能使拦截失效：待决拦截需 durable 判定（events 账本 + 任务最近 agent_done 变迁时间），且关闭监督/撤销凭据时立即恢复原行为（fail-safe 红线）；
4. Provider 不缓存 secret，凭据每次请求从环境解析；provider-specific enablement 在 harness（不建 provider、不采证据）、engine（`should_evaluate`）、provider（`_ask`）三层短路；
5. 语义 State 的每个事实字段必须有真实采集与预算/脱敏测试，否则删除字段而不是留占位。

### 验证命令 / 守护测试

```bash
pytest tests/test_supervisor_interception.py tests/test_supervisor_evidence.py tests/test_supervisor_failsafe.py tests/test_semantic_supervisor.py tests/test_supervisor_policy.py tests/test_decision_providers.py -q
# 期望输出：96 passed（新增 33+ 例：拦截语义 / 全 done 出口网关 / pending 持久拦截 / 真实证据 / 动态 Kill Switch）

pytest -q
# 期望输出：779 passed, 44 subtests passed
```

### 相关文档 / 关联证据

- `herdr/supervisor/harness.py` — `run_checkpoint` 拦截语义、`pending_intervention`
- `herdr/supervisor/evidence.py` — 有界执行证据（loop/git/agent report）
- `herdr/decision/providers/jev.py` — 请求时动态解析 key、`enabled` 短路
- `services/herdr-controller.py` #emit_done_if_allowed / #_supervisor_retry / #_supervisor_attention
- `wiki/semantic-supervisor.md` 红线 #3/#7、`wiki/log.md [2026-09-18] 加固条目`
- PR #60 commit `d84f789`
- 关联教训 §66（回合成本治理）——RateGate 即其产物，本条为其在拦截语义下的对偶约束

---

## 71. 过程持续评估检查点（tests_completed）：事实指纹去重与调用频控正交、过程测试失败抗扰与非终态铁律

### 问题背景

在 Semantic Supervisor 从 V1（仅在任务完成 `agent_done` 挂点评估）向 V1.1（过程持续评估 `tests_completed`）演进时，暴露出四类过程级监督的致命冲突：

1. **事实去重与频控混淆（RateGate 混作 Dedup）**：如果仅依靠 RateGate 时间间隔（如 300s）限流，一旦 Agent 停止产生新测试，时间窗口滑过之后，Controller 会对**完全相同的旧测试结果重复唤起 Jev 评估**；反之，若 Controller 重启导致内存 RateGate 清零，也会无故重评历史旧结果。
2. **频控暂缓导致证据丢失**：当 Agent 密集跑测试时被 RateGate 暂缓，若直接把最新 `evidence_id` 标记为"已处理"或丢弃，会导致当窗口放行后无法评估最新的关键事实。
3. **过程观察篡夺终态控制权（非终态越权）**：在 `tests_completed` 阶段，Agent 内部测试即便全绿、各项语义指标极高，任务实际仍处于编码/提交/自检的进行中。若 Supervisor 在此时返回 `FINISH` 并将任务推进为 `completed`，会直接截断 Agent 的正常交付流程。
4. **过程单轮失败粗暴打断（误判 Agent 循环卡滞）**：Agent 在常规 TDD 或红绿重构中，写新用例或初期报错是必然的正常过程；若仅看到 `tests.failed > 0` 就触发 `RETRY` 重置任务，会形成严厉的"一跑错就打断"，彻底破坏 Agent 的自主修复回路。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 相同测试结果重复调用 Jev | 时间限流不能代替内容身份判定；同一轮测试事实在生命周期内只能评估一次 | 构建基于测试事实（iteration, passed, failed, total, score）的稳定 SHA-256 指纹 `evidence_id`；基于 events 表持久化比对，保证同一指纹全生命周期（含 Controller 重启）只评一次 |
| RateGate 暂缓导致漏评最新测试 | 频控是"推迟执行"，绝不是"判定已消费" | RateGate 触发 skip 时严禁持久化该 `evidence_id`；只更新最后检测到的待决证据，等待窗口冷却放行后精准消费最新事实 |
| 过程检查点直接完成任务 | 过程观察点不是任务终态，`agent_done` 才是交付完成的唯一合法触发源 | Policy 必须具备 Trigger Awareness：在 `tests_completed` 下，任何原本指向 `FINISH` 的决策必须无条件收敛为 `CONTINUE` 放行 |
| 修复过程中的单轮测试失败触发打断重做 | 单轮测试失败 ≠ 卡滞死循环；正在缩减失败数（failed_delta < 0）是明确的正向进展 | 只有连续多轮无改善且卡滞超时才允许考虑干预；只要 Agent 处于活跃工作态或单轮测试呈改善趋势，一律 `CONTINUE` 严禁 RETRY |
| 过程证据采集过重拖垮性能 | 过程评估频率远高于终态；若每次都采集终端 ANSI/transcript 巨量文本，会产生严重 I/O 阻塞 | `trigger="tests_completed"` 下严格走轻量路径：仅解析 `.herdr-loop` 与 `git status/stat`，彻底跳过终端与大文本采集 |

### 操作规范（已固化到 `herdr/supervisor/` 与 `services/herdr-controller.py`）

1. **指纹去重与频控正交分离**：
   - `build_test_evidence_id(test_evidence)`：基于关键事实字典生成 `test_ev_<sha256[:16]>`；
   - `latest_tests_completed_evidence_id(events)`：从持久化 events 中倒序提取已评估的 `evidence_id`；
   - 两者不一致才进入监督流程，进入后才交由 RateGate 判断时间窗口；窗口未到则暂缓且不落库，窗口放行后消费最新证据。
2. **Trigger-aware 策略防篡权**：
   - Policy 输入显式携带 `trigger` 与 `test_progress`（`passed_delta`, `failed_delta`, `score_delta`）；
   - 在 `trigger == "tests_completed"` 下，正向高分一律收敛为 `CONTINUE`；
   - 中间测试失败判断：若 Agent 任务处于 `working` 或 `failed_delta < 0`（改善中），强制 `CONTINUE`，严禁触发 `RETRY`。
3. **Fail-safe 与轻量采集**：
   - 当 `HERDR_SUPERVISOR_ENABLED=false`、`HERDR_SUPERVISOR_JEV_ENABLED=false` 或缺少 API Key 时，Controller 探针即时短路（零 Provider 调用、零多余 I/O）；
   - `collect_execution_evidence(..., trigger="tests_completed")` 跳过终端与报告长文本读取。

### 验证命令 / 守护测试

```bash
pytest tests/test_supervisor_tests_completed.py -q
# 期望输出：10 passed in ~0.25s（A-J 十大专项对抗用例）

pytest tests/ -q
# 期望输出：789 passed
```

### 相关文档 / 关联证据

- `herdr/supervisor/evidence.py` — `build_test_evidence_id` / `extract_test_evidence` / `compute_test_progress`
- `herdr/supervisor/policy.py` — Trigger-aware CONTINUE 降级与过程测试改善保护
- `herdr/supervisor/evaluation.py` — `latest_tests_completed_evidence_id` 扫描去重
- `services/herdr-controller.py` — `check_task_tests_completed` 主循环无感挂点
- `wiki/semantic-supervisor.md` 红线 #8
- 分支 `feat/semantic-supervisor-v1`

---


## 72. 模板新增契约键与既有归一化管道的冲突：双键分家与生命周期复用

### 问题背景

为 Workflow Template 引入 Execution & Context Contract V1（`execution.mode` + `context` 契约）时，需要在不改旧模板行为的前提下把契约、运行期绑定与 Task 记录贯穿 factory → projects → worker → herdr-task 四层：

1. **契约与绑定同名冲突**：模板声明契约 `context: {required, optional}`，运行期绑定也是 `context: {id: path}`。若两者都写入项目 workflow.json 的 `context` 键，`normalize_workflow` 会把绑定 dict 按契约形状重铸（丢路径），或反之污染契约。
2. **Git 硬依赖散布在启动链**：`resolve_project` → `detect_git_root` 在无 Git 目录直接失败，context 模式模板永远走不到 Worker。
3. **Task Workspace 生命周期重复建设风险**：为无 Git 的任务目录另建 cleanup/retention/finalize 通道，会复制 `delete_clone_safely` 一整套已验证的防误删逻辑。

### 根因与解法

1. **双键分家，归一化器不知情**：项目 workflow.json 中契约存 `context`、绑定存 `context_bindings`；StateStore registry 条目（不经 normalize）用 `context` 存运行绑定。归一化只在模板/项目工作流层生效，registry 层原样透传，两个语义各得其所。
2. **模式决策前置到最小切面**：不动 `projects.py` 的 Git 解析链，而是在 shell 层加 `resolve_project_for_template`——先 `load_template` + `execution_mode` 判模式，context 分叉到独立的 `ensure_context_project`，git 路径逐字不变。契约默认值策略同理：`normalize_workflow` 输出显式 `execution: {mode: git}`，但 `execution_mode()` 对任意缺失结构容错返回 `git`，旧数据永远读得出安全默认。
3. **复用 `clones/<task_id>` 物理路径**：context Task Workspace 与 CoW Clone 同根同级注册，stale-heal/删除安全档位/finalize 全部零改动继承；代价是 `clone_path` 字段名对 context 任务语义略宽，换来的是单一生命周期真相。
4. **验收协议跨模式不变**：context 模式以递归文件指纹（全按 untracked 计、过滤内部装配文件）实现 `verify-baseline`，TASK_CHANGED/BASELINE_MATCH 输出协议与 git 模式一致，上层工具无感。

### 验证命令 / 守护测试

```bash
pytest tests/test_execution_context_contract.py -q
# 期望输出：28 passed

pytest -q
# 期望输出：822 passed, 44 subtests passed（baseline 794）
```

### 相关文档 / 关联证据

- `herdr/workflow.py` — `_normalize_execution_contract` / 契约纯函数族
- `herdr/projects.py#ensure_context_project` — Context 项目注册与契约校验
- `services/herdr-worker.py#create_context_task_workspace` — 无 Git Task Workspace
- `docs/product-specs/workflow-template-schema.md` §1.1/§1.2、`wiki/dag-workflow-engine.md` §3.3、`wiki/task-lifecycle.md` §2.1
- 分支 `feat/exec-context-contract-v1`

---

## 73. 门禁完成门禁与 verdict 契约的上下位错配：产物就绪校验必须先问节点类型

### 问题背景

`wf-nexusarchive-0919-01-test-auto-r2`（门禁 test 节点）在 2026-09-20 05:50 已写出合法机器 verdict
（`~/.herdr-controller/gate-verdicts/wf-nexusarchive-0919-01-test-auto-r2.json`：
`{"verdict":"blocked","note":"D1-D3 阻塞：retryRecords 仍传入 null……"}`），
但 Agent 空闲退出后 Controller 打印：

```
[COMPLETION DEFERRED] task=wf-nexusarchive-0919-01-test-auto-r2 required outputs
['测试执行记录','缺陷清单','回归测试结果','边界条件验证情况','测试结论（PASS / FAIL）'] not ready;
treating idle as transient think time
```

任务被锁在 working，最终只能 `status=superseded action=finalized`（日志 634138 行）后另起 `-r3` 重跑——
§62 的自动裁决链路（verdict → `try_auto_verdict` → fix-loop 回流）根本没有机会执行。

根因是两层机制各自正确、组合错位：

1. §31 引入的产物就绪门禁 `check_task_deliverables_ready` 把 `required_outputs` 一律当作仓库相对路径，
   `os.path.join(clone_path, rel)` 后 `os.path.exists` 校验；
2. 模板里门禁节点的 `required_outputs` 是人类契约标签
   （`workflow_templates/software-development-v1.yaml` 135-139 行），永远不可能在 clone 里落成文件——
   `idle → agent_done` 被永久 deferred；
3. §62 的 verdict 契约只在任务到达 `agent_done` 后才被 `try_auto_verdict` 消费，
   于是"产物路径门禁"把"verdict 契约"的整条上位流程饿死。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 产物路径门禁对门禁节点永远为假 | 同一字段（`required_outputs`）在 agent 节点是文件路径、在 gate 节点是契约标签；完成判定必须先分节点语义，再选证据形态 | 门禁节点（`node_is_gate`）在产物就绪门禁上让位于机器可读 verdict：合法 pass/blocked 即视为完成证据 |
| 契约化结论被上游门禁饿死 | 新增"机器可采纳产出物"（§62 verdict）时必须审视链路上所有就绪门禁是否认识这种形态，否则契约永远到不了消费点 | 引入新的完成证据形态时，先枚举 `idle → agent_done` 的全部前置条件并逐一适配 |
| 门禁任务被 superseded 而非收口 | 被 deferred 卡死的门禁任务以"重跑新任务"代替"消费已有结论"，同一结论被重复生产 | verdict 就绪即放行 `agent_done`，复用既有 `try_auto_verdict` 收口 |

### 操作规范（已固化到 `services/herdr-controller.py#gate_verdict_ready`）

1. `handle_event` 的完成物推迟分支改为
   `if not check_task_deliverables_ready(task) and not verdict_ready:`，
   其中 `gate_verdict_ready(task)` 要求：任务属于门禁节点（`node_is_gate`）且
   `read_gate_verdict` 返回 pass/blocked；
2. 门禁节点不新增状态机分支：verdict 就绪只是放行 `idle → agent_done`，
   后续仍走既有 `emit_done_if_allowed` / `try_auto_verdict` 裁决与 fix-loop 回流；
3. 修改门禁语义时必须同时检查 `check_task_deliverables_ready` 与 `gate_verdict_ready` 两处判定，
   不允许只改一处。

### 验证命令 / 证据

```bash
python3 -m unittest tests.test_fix_loop_anti_flapping -v
# 期望：5 passed（含 test_idle_gate_verdict_closes_gate_task_without_literal_output_files）
```

- 现场证据：`~/.herdr-controller/logs/controller.out.log:632907`（COMPLETION DEFERRED）、
  `:634138`（superseded）、`~/.herdr-controller/gate-verdicts/wf-nexusarchive-0919-01-test-auto-r2.json`（05:50 已写出的 blocked verdict）
- PR #67 commit `2427e5c`、`tests/test_fix_loop_anti_flapping.py#ControllerReconcileReworkTest`

---

## 74. 可执行脚本 sys.path bootstrap 第二次复发：死 fallback import 掩盖断裂，复发即升格自动门禁

### 问题背景

§36 已固化规范"独立执行的守护脚本必须在顶部显式注入 `HERDR_ROOT` 到 `sys.path`"，
但当次只修了 `services/herdr-sentinel.py`（配套一条 sentinel 专项用例）。#64（`f4754b9`）
为 `services/herdr-worker.py` 引入 `from herdr.git_coordination import ensure_branch_available` 时：

1. 没有复刻 bootstrap；
2. 写了 `except ImportError: from herdr_git_coordination import ...` 的回退——而
   `services/herdr_git_coordination.py` 在仓库里根本不存在，回退等价于掩埋。

生产路径 `bin/herdr-task:1525` 用 `subprocess.run(worker_cmd)` 以脚本方式从任意 cwd 拉起 worker，
`sys.path[0]` 只有 `services/`：主 import 失败 → 死回退也失败 → worker 直接崩溃。
测试侧 `tests/test_herdr_worker.py` 用 importlib 从仓库根加载模块，sys.path 恰好包含仓库根，
任何测试都不会变红——断裂从 #64 合入后一直静默存在，直到 PR #67（`21feb5e`）才补上。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 同一规范第二次复发（sentinel → worker） | 只修单点、不升格门禁，第二条同构路径必然再次断裂 | 复发即升格：新增 `tests/test_script_bootstrap.py` AST 静态巡检 `bin/*` 与 `services/*.py`，凡 import `herdr` 的脚本必须在其前注入 repo root；纯标准库脚本（如 `herdr-notifier.py`）自动豁免 |
| 死 fallback import 把硬失败变成隐形炸弹 | `except ImportError` 回退到不存在的模块 = 没有回退；测试加载路径又恰好绕开真实执行路径时，故障在提交时不可见 | fallback 目标必须真实存在且有测试覆盖；独立执行路径必须有"以脚本方式、从外部 cwd、剥离 PYTHONPATH"的冒烟测试 |
| 测试导入路径 ≠ 生产执行路径 | importlib 加载模块会注入仓库根到 sys.path，掩盖脚本独立执行时的路径差异 | 守护进程/入口脚本类改动，验证必须包含真实子进程启动（如 `python3 services/herdr-worker.py --help`，cwd 在仓库外） |

### 操作规范（已固化到 `tests/test_script_bootstrap.py`）

1. 新增可执行入口脚本（`bin/`、`services/`）一旦 import `herdr`，必须复刻头部：
   `HERDR_ROOT = Path(__file__).resolve().parent.parent` + `sys.path.insert(0, str(HERDR_ROOT))`；
   静态巡检以 AST 检查"bootstrap 语句先于第一个 herdr import"；
2. 禁止指向不存在模块的 fallback import；回退分支必须有真实实现并被测试覆盖；
3. worker 另配功能冒烟：剥离 PYTHONPATH、cwd 在仓库外执行 `--help` 必须 exit 0。

### 验证命令 / 证据

```bash
pytest tests/test_script_bootstrap.py -q        # 期望：2 passed（静态巡检 + worker 冒烟）
python3 -m unittest tests.test_script_bootstrap # 无 pytest 环境等价执行
```

- PR #67 commit `21feb5e fix(worker): import herdr.git_coordination on standalone launch`
- 先例：§36 行"后台守护服务启动环境依赖脆弱"；
  `tests/test_state_transition_gateway.py#test_sentinel_directly_bootstraps_and_imports_herdr_without_pythonpath`

---

## 75. SQLite 旧 schema 自动升级的跨进程 duplicate-column 竞态

### 问题背景

PR #69 的 Trajectory Ledger 为旧 `events` 表补充 `run_id` 与 `sequence` 列。
原实现先执行 `PRAGMA table_info(events)`，再逐列执行 `ALTER TABLE`；Controller、Sentinel
和 CLI 等独立进程首次打开同一个旧数据库时，两个进程可以同时读到缺列状态，后到者会因
`sqlite3.OperationalError: duplicate column name` 启动失败。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 进程内 `_INITIALIZED_DBS` 无法覆盖独立进程的 schema upgrade 竞争 | schema 检查与 ALTER 之间存在跨进程 TOCTOU 窗口，必须以 SQLite 实际结果为准 | 所有旧库自动升级路径都必须考虑独立连接并发首次初始化，不能只依赖进程内缓存或线程锁 |
| 宽泛吞掉 `OperationalError` 会掩盖锁超时、损坏或语法错误 | duplicate-column 只有在重新检查确认目标列已存在时才代表竞争成功 | 仅允许“duplicate-column + 目标列已存在”通过；其他 SQLite 错误必须继续抛出 |

### 操作规范（已固化到 `herdr/state_db.py::_ensure_event_columns`）

1. 执行 `ALTER TABLE` 时若收到 duplicate-column，立即重新读取 `PRAGMA table_info(events)`。
2. 只有目标列已存在时将其视为另一个进程已完成升级；目标列不存在或错误类型不同则失败。
3. schema upgrade regression 必须使用两个独立进程和独立 SQLite connection，并在旧 schema 快照后强制并发，而不是只用线程锁。

### 验证命令 / 守护测试

```bash
pytest tests/test_state_db_v2.py::test_concurrent_legacy_event_schema_upgrade_is_idempotent -q
# 期望：1 passed；两个 spawned 进程均成功，run_id/sequence 各存在一次且 TrajectoryLedger 可读写
```

### 相关文档 / 关联证据

- PR #69 — Agent Trajectory Ledger schema upgrade follow-up
- PR #69 follow-up commit — 初始跨进程竞态修复
- `herdr/state_db.py::_ensure_event_columns`
- `tests/test_state_db_v2.py::test_concurrent_legacy_event_schema_upgrade_is_idempotent`

---

## 76. 本地 main 过期导致的“前置能力不存在”误判：接单先 fetch，再谈缺件

### 问题背景

Trajectory Observer 任务书声明前置能力（Agent Trajectory Ledger / `run_id` /
`TrajectoryEvent` / `TrajectoryLedger`）已完成。开工时本地 `main` 停在 PR #68
（ab99ed9）：全仓 grep 不到 `TrajectoryEvent`/`TrajectoryLedger`/`run_id`，一度
准备把“前置能力”本身纳入实现范围。`git fetch --all` 后发现 origin/main 已推进到
PR #69（1817f43），且远端存在 `feat/agent-trajectory-ledger` 分支——前置能力早已
合并，只是本地基线过期。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 用本地工作区判断“上游缺件” | 本地仓库只是远端的一个可能过期快照，不是事实来源 | 任何“缺件/不存在”结论必须先排除基线过期，再定方案 |
| grep 不到就认定未实现 | “未实现 / 未合并 / 术语不同”是三种不同情况，处置完全不同 | 先 `git fetch` + 看远端分支与 `git log --all --grep`，再做符号级判断 |
| 发现缺件立刻重造 | 从零实现前置能力会把 PR 膨胀成两期工程，且与上游实现冲突 | 缺件结论必须附“远端同类实现检索”证据，否则视为未验证假设 |

### 操作规范

1. 接单第一步固定执行：`git fetch origin` → `git status` → `git merge --ff-only origin/main` → `git log --oneline origin/main -10`。
2. 关键符号 grep 为空时，补一条 `git log --all --oneline --grep="<关键词>" -i` 与 `git branch -r`，确认远端无同类实现后才认定为缺口。
3. Entry Gate 的“理解和假设”必须写明基线 commit 与同步动作，避免基于过期基线开工。

### 验证命令 / 守护测试

```bash
git merge --ff-only origin/main && git log --oneline -1
# 期望：1817f43 Merge pull request #69 from allinai0506/feat/agent-trajectory-ledger
python3 -c "from herdr.trajectory import TrajectoryLedger; print(TrajectoryLedger)"
```

### 相关文档 / 关联证据

- PR #69（1817f43）— Agent Trajectory Ledger
- `.omc/entry-gate-feat_trajectory-observer-v1.md` — 本轮基线记录
- `herdr/trajectory.py`

---

## 77. 旁路 LLM 观察者的出站泄密面：脱敏必须发生在证据读取的最早时刻

### 问题背景

Trajectory Observer V1 独立评审（round 1）发现 critical C1：`read_log_tail`
对日志只做 ANSI 清洗，未做凭据脱敏；原始日志文本随后出现在三处出站/持久化通道——
Provider 问题体（`question_for` 内嵌 summary）、ObservationContext 的 logs 块、
以及 finding 的 evidence/summary/`metadata.facts` 落库。round 2 又发现
`repeated_action` 的原始 command 文本可经 Provider 问题体与 `metadata.facts`
离开进程。该设计文档原本承诺“凭据形状不离开进程”，但实现只在部分字段上做了截断。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 把“有界（截断/限行）”等同于“安全” | 体积控制不解决凭据泄露，两者是正交属性 | 任何外部文本在进入系统的最早读取点即脱敏，而不是等到落库前 |
| 只检查落库通道 | Provider 的 question/instructions、context、metadata 都是出站面 | 出站面清单化：question 体、context、evidence、metadata 一个都不能漏 |
| 测试只断言最终返回值 | 泄密可能发生在中间通道，返回值干净不代表过程干净 | 密钥回归必须断言 question/state/db/mapping 四通道同时干净 |

### 操作规范（已固化到 `herdr/observer/`）

1. `context.read_log_tail` 读取即 `redact_text`；`engine._consolidate` 对 summary、`suspected_cause`、`metadata.facts` 字符串值、evidence excerpt/signature 做防御性二次脱敏。
2. `signals.question_for` 拼装 Provider 问题前对 summary 脱敏——问题体是独立出站通道。
3. 测试替身必须记录完整 question 体（而不只是 question id），否则问题体泄密无法被断言捕获。
4. 新增任何“把文本送往 Provider 或落库”的路径时，回归测试用真实密钥形状（`sk-...`/`ghp_...`）断言四通道均不含明文。

### 验证命令 / 守护测试

```bash
pytest tests/test_trajectory_observer.py::TestHardeningRegressions -q
# 期望：全部通过；含 test_log_secrets_never_reach_provider_or_store 与
# test_action_command_secrets_never_reach_provider_or_metadata
```

### 相关文档 / 关联证据

- `herdr/observer/context.py:read_log_tail`
- `herdr/observer/engine.py:_redact_evidence`
- `herdr/observer/signals.py:question_for`
- `herdr/supervisor/state.py:redact_text`
- `docs/superpowers/specs/2026-09-20-trajectory-observer-design.md`

---

## 78. 旁路观察者挂上主流程后，测试必须默认关闭它：默认路径回落到生产状态库

### 问题背景

PR #70 把 terminal observation 挂到统一 Done Gateway 后，既有 controller 测试
（`test_supervisor_interception` 的 `t-int-1`/`t-watch-1`、`test_fix_loop_anti_flapping`
的 `test-task-rework-heal`）在调用 `handle_event`/`emit_done_if_allowed` 时触发了默认
Observer 调度器；当 `store=None` 时 `TrajectoryObserver` 回落到
`state_db.get_default_db_path()`，即生产库 `~/.herdr-controller/state.db`，实际写入
3 行 `trajectory_findings` 测试残留。更隐蔽的是：在 APFS clone 中运行测试同样会写
生产库——默认路径基于 `HOME`，与仓库位置无关。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 新挂到主流程的旁路能力会被大量既有测试间接触发 | "旁路"只是生产语义；在测试里它是新增的全局副作用源 | 新增全局副作用（观察/写入/子进程/网络）必须同步提供测试级 kill switch，并在 conftest 默认关闭 |
| 用例用 tmp_path 隔离 DB，但默认路径仍指向 HOME | tmp_path 只覆盖显式传 store 的用例；`store=None` 的默认路径必须单独审计 | 任何"默认 DB 路径"型副作用都要在 conftest 用 env 关闭，不能依赖每个用例自觉 |
| 评审/克隆里跑测试也会写生产库 | 测试隔离与仓库位置无关（HOME 决定路径） | 在 clone 里跑测试前必须设 `HERDR_STATE_DB` 指向临时文件 |

### 操作规范（已固化到 `tests/conftest.py`）

1. conftest **硬覆盖** `os.environ["HERDR_OBSERVER_ENABLED"] = "0"`（不是 setdefault：开发者 shell 导出 1 也不能让测试重新写生产库）；
2. 需要真实默认调度器的用例显式 `setenv("HERDR_OBSERVER_ENABLED","1")` + `HERDR_OBSERVER_LIVE_PROBE=0` + `HERDR_OBSERVER_CONFIG` 指向不存在路径；
3. 未显式传 config 的 `ObservationScheduler` 测试改为显式 `_base_config()`（测试自包含）；
4. 新增回归 `TestTestEnvironmentIsolation` 断言测试环境默认禁用且 submit 返回 False。

### 验证命令 / 证据

```bash
pytest tests/test_trajectory_observer.py -q          # 91 passed
pytest tests/test_supervisor_*.py tests/test_fix_loop_anti_flapping.py -q  # 104 passed
# 生产库 trajectory_findings：清理前 3 → 清理后 0；复跑 195 项测试后仍为 0
```

- pre-fix RED：`assert True is False`（测试环境默认 enabled=True）。

### 相关文档 / 关联证据

- PR #70 merge `aebbd69`（引入该挂载）
- `tests/conftest.py`
- `tests/test_trajectory_observer.py:TestTestEnvironmentIsolation`
- `services/herdr-controller.py:_observer_terminal_checkpoint`

---

## 79. 终端可见输出（Screen Marker）做门禁裁决的“Prompt 回显击穿”与字串误杀防御

### 问题背景

在工作流门禁节点（如 `test-auto` / `review`）调度中，Controller 通过 `_verdict_from_screen()` 解析 Pane 可见屏幕输出。历史实现仅通过 `line.split("HERDR_GATE_VERDICT:", 1)[1].strip().split()[0]` 截取首词并转为 `pass`/`blocked`。
当 Agent 工位启动时，终端回显 Prompt 中的契约说明文本：
`HERDR_GATE_VERDICT: pass   或   HERDR_GATE_VERDICT: blocked`
由于首词恰好是 `"pass"`，Controller 在任务启动 0~2 秒内（因短暂 idle）通过 `gate_verdict_ready` 判定门禁已通过，立即触发 `auto_verdict_and_finalize_if_ready`，将任务标记为 `completed -> cleaned`。导致：
1. 测试/审查 Agent 尚未开始执行实际任务，工位即被提前清理，质量门禁被直接击穿（False Positive Bypass）；
2. 用户在控制台和工作流状态中无法看到运行中的工位（Pane 早已被销毁）；
3. 在第一版修复尝试中，引入了包含子串的对立词检测（`"ok" in line`），导致包含 `"smoke"`、`"token"`、`"broken"` 的合法阻塞报告（如 `smoke test failed`）被误杀过滤；且整行对立词集合检查导致合法说明（如 `pass - 0 tests failed`）被误判为模板歧义丢弃。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 下发给 Agent 的 Prompt 包含机器契约标记的完整字面量 | Agent 终端回显 Prompt 是常态，字面量与真实产出无从区分 | 下发契约模板必须使用语法占位符（如 `<pass\|blocked>`），禁止在说明中出现直接可匹配的字面行 |
| 仅取标记后的首词作为裁决 | 指令行、二选一讨论行、思考行可能首词也是目标词 | 必须在行级别过滤单行多标记、二选一指示词（`或`/` or `/` / `）及占位符；且对第一词后的后续 token 校验冲突词 |
| 文本过滤使用子串检测 (`"ok" in line`) | 英文词根广泛重叠（`smoke`/`token`/`broken` 均含 `ok`） | 必须使用严格单词边界或 `set(tokens)` 进行离散词比对，禁止子串模糊匹配 |
| 过于宽泛的整行对立词判定 | 真实的裁决常伴随否定式说明（`0 tests failed`、`did not fail`） | 裁决行过滤不得全行匹配 `fail` 等泛化解释词，冲突检查仅限定于对立裁决关键字（`blocked` vs `pass`） |

### 操作规范（已固化到 `services/herdr-controller.py` 与 `herdr/direct_dispatch.py`）

1. **Prompt 模板占位符化**：`herdr/direct_dispatch.py` 中将契约格式从 `HERDR_GATE_VERDICT: pass 或 HERDR_GATE_VERDICT: blocked` 调整为 `HERDR_GATE_VERDICT: <pass|blocked>`；
2. **歧义行过滤纯函数**：`_is_instructional_or_ambiguous_verdict_line()` 在行级别过滤多 marker、二选一指示词（`或`, ` or `, ` / `, `二选一`, `示例`, `格式`, `template`, `<pass`, `[pass` 等）；
3. **精准 Token 冲突校验**：`_verdict_from_screen()` 提取第一词后，仅对其后续 token 集合 `trailing` 检查是否存在对立裁决词；
4. **正向与对抗回归门禁**：在 `tests/test_auto_acceptance.py` 中固化 7 组回归用例，涵盖 Prompt 回显忽略、斜杠/二选一忽略、`smoke` 词汇免误杀、带解释合法裁决放行、以及回显与正式结论共存的生产场景。

### 验证命令 / 证据

```bash
pytest tests/test_auto_acceptance.py -k test_screen_marker -q  # 8 passed
pytest tests/test_auto_acceptance.py -q                       # 33 passed
pytest -q                                                     # 981 passed, 44 subtests passed
```

- pre-fix RED：`FAILED test_screen_marker_ignores_prompt_template_with_alternatives` (`AssertionError: 'pass' is not None`).

### 相关文档 / 关联证据

- 关联缺陷：`wf-nexusarchive-0921-01` 任务秒级被放行并 clean
- 关联代码：[`services/herdr-controller.py`](file:///Users/user/haflow/services/herdr-controller.py#L3205), [`herdr/direct_dispatch.py`](file:///Users/user/haflow/herdr/direct_dispatch.py#L61)
- 关联测试：[`tests/test_auto_acceptance.py`](file:///Users/user/haflow/tests/test_auto_acceptance.py#L287)
- 关联历史：lessons §62（门禁 verdict 契约化）、§73（门禁产物就绪校验上下位错配）

## 80. Run 成功事实与 Task 生命周期状态不可混用

### 问题背景

Harness Metrics V1 最初用 `final_status == "completed"` 推导
`task_completed`。HAFlow 的 Task 在成功后还会继续经过
`committed → integrated → cleanup_ready → cleaned`，甚至可能在成功 Run
之后进入 `superseded`。因此一个已经产生 `run_completed` 的 Run，会被错误
报告为 `task_completed=false`。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 用当前 Task 状态代替历史 Run 事实 | Runtime State 表达当前生命周期，Trajectory 表达历史发生过什么 | 统计“Run 是否成功完成过”时必须优先读取 `run_completed` 事实 |
| 只判断 `completed` 单一状态 | 成功任务存在多个完成态 | 复用 `COMPLETED_TASK_STATUSES`，禁止复制状态集合 |
| `task_completed` 与 `final_status` 混为一谈 | 一个是 Run 成功事实，一个是 Task 当前状态 | Metrics 同时保留两者，分别表达历史成功与当前生命周期 |

### 操作规范（已固化到 `herdr/metrics.py`）

```python
task_completed = bool(facts["run_completed"]) or final_status in COMPLETED_TASK_STATUSES
```

新增或修改 Run/Task 指标时，先明确字段是历史事实还是当前投影；跨越
Task 生命周期的指标必须用 Trajectory 与状态机常量联合判断。

### 验证命令 / 证据

```bash
pytest -q tests/test_metrics.py -k lifecycle  # 3 passed
pytest -q tests/test_metrics.py tests/test_harness_metrics_cli.py  # 10 passed
pytest -q  # 1055 passed, 44 subtests passed
```

回归覆盖 `committed`、`cleaned`、`superseded` 三种状态在已有
`run_completed` 事实下均返回 `task_completed=true`。

### 相关文档 / 关联证据

- `herdr/metrics.py#get_run_metrics`
- `herdr/transitions.py#COMPLETED_TASK_STATUSES`
- `herdr/trajectory.py#TrajectoryLedger`
- `tests/test_metrics.py#test_run_completed_remains_completed_across_task_lifecycle`

---

## 81. 聚合读模型的身份归属与 SQLite JSON 短路：两个让 Metrics 静默出错的边界

### 问题背景

Harness Metrics V1 合并后独立验证发现两处边界：

1. `herdr/metrics.py` 直接用轨迹事件里的 `task_id` 取 Task 行并采用其 `status`，未校验该 Task 的持久化 `run_id` 是否属于当前 Run。实测：Run A 的事件引用 Run B 的 Task（completed）→ 查询 Run A 得到 `final_status=completed`、`task_completed=true`，并把 Run B 的 `task_id/workflow_id` 拼进 Run A 的指标（重新 launch 后查询旧 Run 的典型场景）。
2. `herdr/state_db.py` 对 `verification_completed` 行使用裸 `json_extract(payload_json, ...)`；实测一条损坏 `payload_json` 即 `OperationalError: malformed JSON`，CLI exit 1，该 Run 全部指标不可得。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 事件携带的 `task_id` 被当作本 Run 身份 | "事件里有 task_id" ≠ "该 Task 属于这个 Run"；聚合读模型同样要做归属校验 | 用 `run_id_for_task(task) == run_id` 判定（legacy 无 run_id Task 回退 `run_<task_id>`），不匹配时不借用身份与状态 |
| `json_valid(x) AND json_extract(x, ...)` | SQLite 的 `AND` 不保证短路，malformed 输入仍可能抛错，且会让**整条聚合查询**失败 | 必须写成嵌套 `CASE WHEN json_valid(x) THEN json_extract(...) END`；损坏行仍计入 total，只无法归类 |

### 操作规范

```python
if task is not None and run_id_for_task(task) != run_id:
    task = None  # 不借用其他 Run 的身份与状态
```

```sql
SUM(CASE WHEN event_type = 'verification_completed'
         THEN CASE WHEN json_valid(payload_json)
                   THEN CASE WHEN json_extract(payload_json, '$.verification.passed') = 1
                             THEN 1 ELSE 0 END
                   ELSE 0 END
         ELSE 0 END)
```

### 验证命令 / 证据

```bash
pytest -q tests/test_metrics.py -k "cross_run or malformed"          # 2 passed（修复前 2 failed）
pytest -q tests/test_metrics.py tests/test_harness_metrics_cli.py   # 12 passed
HERDR_STATE_DB=<tmp>/state.db python3 bin/herdr-task metrics --run-id run-mine --json
# → task_id/workflow_id/final_status 为 null，task_completed=false
HERDR_STATE_DB=<tmp>/state.db python3 bin/herdr-task metrics --run-id run-badver --json
# → exit 0，verification_total=2 passed=0 failed=1
```

### 相关文档 / 关联证据

- `herdr/metrics.py#get_run_metrics`
- `herdr/state_db.py#aggregate_run_metric_rows`
- `tests/test_metrics.py#test_cross_run_task_identity_is_never_borrowed`
- `tests/test_metrics.py#test_malformed_verification_payload_degrades_without_failing`
- `herdr/state_db.py#_task_matches_run`（既有同类判据先例，严格 `run_id_for_task` 语义）

## 82. Action 执行必须把唯一身份与副作用 claim 放进同一持久化边界

### 问题背景

Supervisor 的 PolicyDecision 原本直接调用 Controller handler。仅靠
`if not exists: insert` 或事件扫描无法阻止两个 Controller 同时执行
同一个 RETRY，也无法在 Controller 崩溃后区分“已请求”与“已完成”。

### 经验教训

Intervention 的逻辑身份必须由数据库唯一约束保护，执行前必须使用
事务原子 claim；执行结果和失败也必须成为同一 StateStore 中的正式事实。
恢复逻辑还要先检查 Task 当前状态，才能在副作用已应用后安全补记完成，
避免以“恢复”为名再次触发同一动作。

### 验证

`tests/test_intervention_store.py` 使用两个 SQLite 连接并发创建同一
decision；`tests/test_action_protocol.py` 使用两个 Controller worker
并发 claim，并覆盖 running RETRY 的崩溃恢复。

## 83. 外部 Action 的 dispatch receipt 必须先于事实消费

### 问题背景

Action Protocol V1 的 VERIFY/RETRY 都跨越 Controller 与 Agent/Pane
进程边界。复核发现：仅写入 Task 状态不能证明 Action 已执行；读取
`EVAL_DONE.json` 后再次读取文件做 freshness 判断也会产生 TOCTOU；而
`verification_completed` receipt 写失败后若继续 Supervisor evaluation，
当前 evidence 会被 dedup 消费并永久丢失。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| Task 已是 `rework` | 状态是投影，不是本次 Intervention 的执行证据 | 用当前 `intervention_id` 对应的 dispatch receipt 判定已执行 |
| EVAL_DONE 两次读取 | metrics 与 freshness 可能来自不同文件版本 | 单次字节快照同时生成 metrics、hash、完成时间 |
| receipt 持久化失败仍继续 | evidence 被 dedup 后无法恢复 | receipt 成功落盘前不得消费或记录 evaluation dedup |
| 历史 failed VERIFY 阻塞新 episode | 失败事实跨 episode 泄漏 | 用新的 `agent_done` 边界限定 active Intervention |

### 操作规范

跨进程 Action 使用“durable intent → external dispatch → durable receipt”
顺序；恢复时只对当前 `task_id` 查询和 claim。RETRY 必须有
`retry_dispatched`，VERIFY 必须有 `verification_dispatched` 及后续新
verification receipt；Task status 本身不能替代这些事实。

### 验证

`tests/test_action_protocol.py` 覆盖 task-scoped recovery、历史 failed
VERIFY episode、RETRY 无 dispatch 阻断和 kill switch；
`tests/test_supervisor_tests_completed.py` 覆盖 receipt 写失败不消费
evidence；本轮全量 `pytest -q` 为 1101 passed、44 subtests。

## 84. 内环质量门禁必须对存量 lint 债务做基线分诊：只拦新增，不拦全仓

### 问题背景

`wf-haflow-0923-01-test-auto` 在测试全绿（专项 21/21、全量 1122 passed）
的情况下被判 `blocked`：`herdr/evaluator.py` 的 `is_converged` 要求
`lint_errors == 0`，而默认 lint 命令是全仓 `ruff check .`，主干基线本身
就有 2696 个存量错误。`quality = 100 - 10 × lint` 直接归零，
`composite` 只有 65/100，5 轮内环必然耗尽并升级总指挥仲裁。
这是系统性误杀，不是 Agent 实现缺陷：任何工作流都会在同一门禁上卡死。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 全仓 `ruff check .` 要求零错误 | "当前 2562 个错误" ≠ "本次新增 2562 个缺陷"；存量债务不能计入本轮质量分 | 门禁只看增量 `new = max(0, current - baseline)`，观测总数仍全量记录 |
| 基线只在口头，不在持久化 | 没有落盘的基线等于没有基线，复评无法重现同一判定 | `auto_init_task_loop` / `herdr-loop init` 在 init 时快照一次 `BASELINE_LINT.json`，eval 只读不写 |
| 无基线旧 clone | 缺基线不得改变既有语义 | 缺文件/损坏时回退绝对门禁（`new=None` 即按原 `lint_errors` 判定） |

### 操作规范

```python
# herdr/evaluator.py：纯函数，数字进、判定出；IO 留在 bin/ 装配层
new_lint = effective_defects(current_lint, baseline_lint)  # 永不为负
quality = max(0.0, 100.0 - (new_lint * 10.0 + new_type * 15.0))
# is_converged 看 new_*（None 时回退看绝对值，保持旧 clone 兼容）
```

`bin/herdr-task:auto_init_task_loop` 与 `bin/herdr-loop:init` 快照基线
（120s 超时、best-effort，失败只告警不阻断派发）；
`bin/herdr-loop:run_evaluation` 读取基线并透传；
`EVAL_DONE.json` / `METRICS.json` 新增
`baseline_lint_errors / new_lint_errors`（加法兼容，Supervisor 白名单读取不受影响）。

### 验证命令 / 证据

```bash
pytest -q tests/test_loop_evaluator.py tests/test_inner_loop_convergence.py tests/test_inner_loop_protocol.py tests/test_outer_loop_flow.py  # 38 passed
pytest -q  # 1107 passed, 44 subtests passed
# 真实链路：tmp clone init(lint 报 5 存量) → 快照 baseline=5 → eval 100.0 CONVERGED；
# lint 改报 6 → new=1 → 96.5 正确阻断
```

### 相关文档 / 关联证据

- `herdr/evaluator.py#effective_defects`、`#write_baseline_lint`、`#read_baseline_lint`
- `bin/herdr-loop#run_evaluation`、`bin/herdr-task#auto_init_task_loop`
- `tests/test_loop_evaluator.py#test_baseline_debt_does_not_block_convergence`
- `tests/test_loop_evaluator.py#test_new_lint_still_blocks_convergence`

## 85. Fix-loop 三类死锁：通知丢失、回流无界、作废后空推进

### 问题背景

`wf-haflow-0923-01` 在 test 门禁三连 blocked 后进入零 live 任务死停，
实测链条（`controller.out.log` 原文可查）：

1. `test-auto-r3` 走完 `agent_done→completed→cleaned`，`AUTO VERDICT blocked` →
   fix-loop 作废 → `QUEUED loop=3/4` → `_handle_fix_loop_item` 等总指挥 120s →
   `[FIX LOOP WAIT TIMEOUT]` 直接 return，通知丢弃无重试；
2. `FIX_LOOP_MAX` 默认 3 只透传显示不生效，实际走到 `loop=4`；
3. `impl-fix2` 被熔断 supersede 后，sweep 以陈旧 T1 完成判定
   implementation complete，直接 advance 到 test 重测同一候选 → 必 blocked →
   再作废，确定性空转。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 恢复通知 fire-and-forget | 恢复关键路径的消息丢失 = 死锁，必须持久化 + 补投 | 超时转 attention episode（`coordinator_busy` + 退避），sweep 在总指挥 idle 后补投 |
| 预算只显示不执行 | 不 Vergleich 的计数器等于没有预算 | 作废前先比 `count >= max_loops`，超限只升级不作废；同 verdict 指纹直接升级 |
| 作废后不设闩 | 陈旧完成会让 sweep 越过待重做节点空推进 | 作废即设 `pending_redo` 闩，有真正重做完成才清除（含计数/指纹/升级记录 for 下一轮） |
| 升级记录不清零条件 | 新判据会被旧升级静默吞掉 | 升级记录带 verdict 指纹，指纹变化即重新升级 |

### 操作规范

```python
# 决策纯函数收敛 herdr/fix_loop.py（无 IO，标准库 only）；
# services/herdr-controller.py 只做编排装配
handle_fix_loop: 预算/指纹门禁 → 作废 → 计数+闩+指纹 → 入队
_handle_fix_loop_item: 120s 超时 → attention 持久化（不再直接丢弃）
check_all_workflows_stage_advance: 每轮 redeliver_pending_fix_loop
advance 双路径: 依赖有闩且无闩后完成 → 跳过（一次性日志）
```

### 验证命令 / 证据

```bash
pytest -q tests/test_fix_loop_recovery.py  # 23 passed（16 纯函数 + 7 装配）
pytest -q  # 1130 passed, 44 subtests passed
# 场景串联：max_loops=1 时第 1 轮作废入队 → 第 2 轮升级不作废 → 第 3 轮静默
```

### 相关文档 / 关联证据

- `herdr/fix_loop.py`（新增，~200 行纯函数）
- `services/herdr-controller.py#handle_fix_loop`、`#_handle_fix_loop_item`、`#redeliver_pending_fix_loop`、`#_fix_loop_latch_blocks`
- `tests/test_fix_loop_recovery.py`
- 事故现场：`wf-haflow-0923-01`（test-auto-r3 / impl-fix2 / FIX LOOP WAIT TIMEOUT）

## 86. 直派必须携带候选分支：测试测错分支的 verdict 毫无信息量

### 问题背景

`wf-haflow-0923-01-test-auto-r6` 被 `STAGE ADVANCED DIRECT` 派发时丢了
`--onto`，clone 停在 main（`3be4362`）而非 T1 特性分支，verdict
“候选无实现改动”恒成立，白烧一轮。与此同时 fix 任务以默认
`integration-mode none` 运行，`impl-fix4` 的 7 个文件（含 review 点名的
架构文档）以未提交形态 stranded 在 retained clone 里——和 fix1 同一剧本。

根因两处都在“分支上下文掉了”：`context_branch` 只写进 prompt 备注，
从不进 launch 命令；fix-loop 消息模板的 launch 骨架缺
`--integration-mode git`。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 测试节点用了本节点旧分支 | 候选分支必须取自依赖链（实现分支），不是本节点 | `candidate_branch_for_node` 依赖优先、本节点回退 |
| 非法分支值进 shell 命令 | 分支名是外部输入，必须先消毒 | `sanitize_branch_name`，非法即无 onto（fail-open） |
| 空候选也进内环 | 无差异的候选必 blocked，烧 5 轮毫无意义 | 派发前 `rev-list base..onto` 预检，全空即 fallback |
| fix 默认不落分支 | integration none 的 fix 对候选贡献恒为 0 | 消息模板默认 `--integration-mode git` |
| supersede 丢 WIP | 作废≠删除工作，WIP 必须先落盘 | 作废后 best-effort auto-commit（不含内部目录，不 push，不阻断） |

### 操作规范

```python
# herdr/direct_dispatch.py（纯函数）：spec 携带 onto_branch
# services/herdr-controller.py：launch 透传 --onto；空候选 fallback
# bin/herdr-task#supersede_task：作废成功后 _autosave_clone_wip（warn-only）
```

### 验证命令 / 证据

```bash
pytest -q tests/test_dispatch_candidate.py  # 11 passed（planner/装配/超集）
pytest -q  # 1141 passed, 44 subtests passed
```

### 相关文档 / 关联证据

- `herdr/direct_dispatch.py#candidate_branch_for_node`、`#sanitize_branch_name`
- `services/herdr-controller.py#_dispatch_candidate_ready`
- `bin/herdr-task#_autosave_clone_wip`
- `tests/test_dispatch_candidate.py`
- 事故现场：`wf-haflow-0923-01-test-auto-r6`（测 main）、`impl-fix4`（7 文件 stranded）

## 87. Intent 存在不等于已送达：sender 失败与 crash 恢复必须走不同分支

### 问题背景

Collaboration Protocol V1 复用 Supervisor 的
`dispatch_intent → prompt → dispatched` durable 模式。S6 round 1 独立评审
用实证抓到阻塞缺陷 D1：`dispatch_collaboration_event` 在 sender 显式抛错后，
intent 已落盘，重试时命中“既有 intent 即恢复”分支，零调用 sender 直接返回
`dispatched/recovered=True`——交付从未发生却被记为送达（幽灵 dispatched）。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| intent 存在即视为已发送 | intent 只证明“尝试开始”，不能证明“对方收到”；crash-after-success 与 fail-before-send 共用同一分支必然误判其一 | 恢复分支仅用于 crash（无失败证据）；sender 显式失败必须落终态 `failed`，不得留可恢复的 `created` |
| 失败留 `created` 等重试 | 重试命中 intent 恢复分支，失败被洗成成功 | 失败即 `mark_failed`（终态，不再重发）；重发需求由上游按新 `source_fact` 发起新事件 |
| 超长 refs 截断丢关联 ID | 先拼全文后截断，切掉的恰是尾部 `HANDOFF_ID` | 先截 body（refs 封顶 10×200）再追加 ID 尾，截断永不断关联 |
| 缺身份时合成 `run_<task>` | 合成身份让 fail-closed 变成 fail-open，跨 run 串扰 | 缺 run_id 取共享 workflow 域，再缺返回 None 并 mark failed，永不猜测 |

### 操作规范

```python
# services/herdr-controller.py#dispatch_collaboration_event
# prior intent → 补标 dispatched + recovered（不重发）
# sender 异常 → mark_collaboration_failed（终态）
# 缺 pane / 跨 run / 缺 run → failed，永不 fallback
# herdr/collaboration.py#build_handoff_prompt：先截 body，后保 HANDOFF_ID 尾
```

### 验证命令 / 证据

```bash
pytest -q tests/test_collaboration.py tests/test_collaboration_store.py tests/test_collaboration_dispatch.py tests/test_collaboration_e2e.py tests/test_collaboration_wiring.py  # 36 passed
pytest -q  # 1238 passed, 44 subtests passed
```

### 相关文档 / 关联证据

- `herdr/collaboration.py#identity_key`、`#build_handoff_prompt`、`#collab_run_for_task`
- `herdr/state_db.py#create_collaboration_event`（identity 唯一 + canonical 重读）
- `services/herdr-controller.py#dispatch_collaboration_event`、`#maybe_dispatch_node_handoffs`、`#maybe_ack_on_working`
- `docs/architecture/collaboration-protocol.md`（Recovery 取舍已记录）
- 同类模式：`docs/lessons/lessons-learned.md` §83（dispatch receipt 先于事实消费）

## 88. 跨 Task 协作的隔离域不能用 Task 自身的执行身份

### 问题背景

PR #89 的 `collab_run_for_task` 首选 `task.run_id` 做 handoff 隔离域。
但 `bin/herdr-task:1926` 每次 launch 都生成独立 `run_id`，同 Workflow 的
Implementation 与 Review Task 的 run 天然不同 → 生产 dispatch 恒失败。
测试因构造的 Task 没有独立 run_id 而回退到共享 workflow_id，假绿掩盖。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 用 Task 执行身份做跨 Task 隔离域 | 跨 Task 事实的隔离域必须是两者共享的上级执行身份，不是各自的执行身份 | V1 取 `workflow_run_id` / `execution_id` / `workflow_id`，永不取 `task.run_id`；缺失即 fail-closed |
| 测试 Task 缺少生产字段 | 缺字段的测试替身会走 fallback 分支，与生产走不同代码路径 | 隔离/身份类测试必须携带生产必填字段（此处为各异的 `run_id` + 共享 `workflow_id`） |

### 验证命令 / 证据

```bash
pytest -q tests/test_collaboration_dispatch.py  # 含异 run_id 同 workflow 可派发、跨 workflow 拒绝
pytest -q  # 1241 passed, 44 subtests passed
```

### 相关文档 / 关联证据

- `herdr/collaboration.py#collab_scope_for_task`
- `bin/herdr-task:1926`、`1951`
- `docs/architecture/collaboration-protocol.md`（Run isolation）

## 89. 收尾节点的分支不是交付物分支：链末端交付 + 有锚任务空终化的双重约束

### 问题背景

`wf-haflow-0923-02`（Git 终化收编 HEAD，候选
`agent/opencode/fix-wf-haflow-0923-02-impl-fix2@33a1f1d`，base `c66fc46`）收尾时，
收尾节点 `wrapup-t1` 的分支 `agent/claude/docs-wf-haflow-0923-02-wrapup-t1` 被三件事同时拉扯：
交付 PR 从哪条分支开、知识沉淀提交落在哪、以及收尾任务**自身**的 git 终化锚点。

实测拓扑：`git merge-base --is-ancestor c66fc46 33a1f1d` 为假，
`git rev-list --left-right --count c66fc46...33a1f1d` 为 `1  2`，merge-base 是 `adf8a32`——
候选**不是 base 的后代**：`adf8a32`（fix2）已由 PR #92 合入 `main`，而 base `c66fc46` 正是
`46a31f1` 与 `adf8a32` 的**合并提交**，候选只是在这条旧线上继续提交。于是「把候选 merge 进
收尾节点分支、由收尾分支一次性交付」这条最直觉的路径，被本工作流自己刚交付的 fail-closed
守卫堵死：`herdr/git_adoption.py` 的 `merge_commit_in_range`(:338/:490) 与
`baseline_not_ancestor`(:287) 会把「区间内含合并提交」或「baseline 非 HEAD 祖先」的收编
一律 REFUSED。

第二重约束来自收尾任务自己的终化记录：`wrapup-t1` 带 `baseline_commit=c66fc46`（锚点存在）。
若收尾节点分支零提交，终化判 EMPTY，而 `_empty_auto_releasable`（H-3）对**有锚任务恒返回
False** → 收尾任务自己 `finalize_escalated` → `close_workflow` 撞第二道闸 `escalated_git`
（H-1），整个工作流最后一步卡在「需人类显式确认」上。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 把「交付分支（集成分支链末端）」读成「本节点分支」 | 收尾节点分支是**终化锚点**，不是交付链末端；两者可以同名不同源 | 交付 PR 的 head 必须是链末端提交所在的分支（此处 `...impl-fix2@33a1f1d`）；收尾节点分支只承载收尾文档提交 |
| 收尾节点分支零提交 | 有锚任务的 EMPTY 不再自动放行（H-3），零提交会把收尾任务自己推进 escalation | 收尾必须在本节点分支留下至少一个实质提交（知识沉淀 + wiki 回填）后再报完成 |
| 想把候选并入收尾分支以「带着交付物一起交付」 | 收编侧对 merge 提交与 baseline 非祖先 fail-closed，合并只会让本节点终化被 REFUSED | 禁止在收尾分支 merge/rebase 交付分支；同名分叉用内容等价判定（§87），权威 head 由 PR 指向决定 |
| 以为「推交付分支」= 推自己的分支 | 收编侧把「出现在任意 `origin/*` 可达集合」当外来源（wf-haflow-0923-02 review-t2 F-1） | 只推链末端交付分支；收尾节点自己的分支不推 origin，避免自身终化被判外来 |

### 操作规范

1. 先固定交付物身份：基准分支显式传入（收尾脚本 `--base <base_branch>`），并核对候选相对 base
   的提交数 > 0（零提交分支会被 `git cherry` 误判为已合入）。
2. 知识沉淀 / wiki 回填写**两次相同内容**：一次在链末端交付分支（进 PR），一次在收尾节点分支
   （供本节点终化收编）；**新增条目逐字一致**，两分支既有的历史行差异不得回灌（本轮
   `wiki/log.md` 的 `<##` 修复属候选自身改动，收尾分支不回灌）。
3. 交付 PR 只做「推送链末端分支 + 创建 PR」，禁止合并、禁止 `--force` / `--yes`。
4. 收尾节点自己的分支**不推送**（见 F-1：推送会让自身终化被判 `foreign_commit_in_range`）。

### 验证命令 / 证据

```bash
# 交付物身份与拓扑（只读）
git merge-base --is-ancestor <base> <candidate>; echo $?    # 1 ⇒ 候选不是 base 后代
git rev-list --left-right --count <base>...<candidate>      # 1  2
git show-ref --verify --quiet refs/heads/<delivery_branch>  # 分支存在才可跑收尾脚本

# 有锚任务空终化 / close 第二道闸的行为断言
pytest -q tests/test_t3_probes.py::L2EmptyReleasable \
          tests/test_t3_probes.py::M3CloseWorkflowGate \
          tests/test_finalize_empty.py::FinalizeEmptyTest  # 10 passed
```

- 本轮独立复证（收尾 Agent 自测，非实现方自述）：`pytest -q` **1321 passed + 44 subtests**、
  `ruff check` 基线 `c66fc46` 2768 == 候选 `33a1f1d` 2768 且 `(文件,规则)` 多重集差异为空。

### 相关文档 / 关联证据

- `services/herdr-controller.py#_empty_auto_releasable`（H-3：有锚任务 EMPTY 不放行）
- `bin/herdr-task#close_workflow`（H-1：`escalated_git` 闸门；`unsettled_git` 已排除 `finalize_escalated`）
- `herdr/git_adoption.py`（`baseline_not_ancestor` / `merge_commit_in_range` / `foreign_commit_in_range`）
- `tests/test_t3_probes.py#L2EmptyReleasable`、`#M3CloseWorkflowGate`、`tests/test_impl_fix4_regression.py`
- `wiki/dag-workflow-engine.md` §12/§13；shared notes `n-1790229087315-5429`（review-t2）、
  `n-1790228635198-359f`（test 门禁 pass）
- 同类模式：`docs/lessons/lessons-learned.md` §87（收尾条目固定交付物身份 / 分叉内容等价性）

---

## 90. Fix-loop 证据门禁：采样、episode 与候选身份必须可重放

### 问题背景

`wf-haflow-0924-01` 的独立 test gate 在候选 `4721d6b` 上稳定复现了五类阻断：
完成观察在任务 epoch 变化后把 `first_seen` 留在 NULL；两个 Controller sweep
可对同一 blocked episode 各发一次重推；同刻 delivery 候选按 note_id 静默择一；
`--force` 被额外确认参数破坏；opt-out 审计异常越过路由边界。修复过程中还在
`fbebbb1` review 锚点复现了崩溃观察无消费者和过期 action lease 残留。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 读两次 marker 就能完成 | 采样必须是同一 task/version epoch 的持久状态机，且两次有效样本至少间隔一个轮询周期 | `StateStore.observe_completion` 持久化采样；Controller 只用 `expected_status + expected_version` 的事务 CAS 提交 |
| episode 只在单个进程内记计数 | JSON 读改写和外部 prompt 之间存在竞态，单纯 `attention.json` 不是动作锁 | 先用跨进程文件锁原子 claim，再发送；失败保留可恢复状态，预算耗尽只允许一次人工升级 |
| 候选按时间/字典序选择 | append-only 记录的身份边必须先解析；同身份冲突、未知 supersede、同刻多候选都应拒绝 | `delivery_record` 以显式 identity/alias 图选择唯一有效候选，失效 replacement 不回退 predecessor |
| 审计 best-effort | 隔离 opt-out 没有可验证回执就等于没有授权 | 审计异常、空 review 池和未知 delivery identity 均 fail-closed；失败 Task/event/workflow metadata 必须在 topology/Pane 前落盘 |
| 修复测试只调用 helper | 状态、路由和 CLI 边界之间的接线错误仍会进入生产 | 每个 blocker 至少有一条经过真实 StateStore/Controller/CLI 与临时 SQLite/共享文档的回归；并发用独立连接/受控交错 |

### 验证命令 / 关联证据

- 修复前专项复现：`tests/test_impl_fix4_blocker_regression.py` 的 FR-1/FR-2/FR-4/FR-6 用例分别以 exit 1 暴露 NULL epoch、重复 repush、身份冲突和审计异常；FR-5 在隔离 `4721d6b` 快照运行 `tests/test_t3_probes.py::M3CloseWorkflowGate::test_accept_escalated_or_force_or_abandon_closes`，exit 1（直接 `--force` 被 `SystemExit(2)` 拒绝）。
- 修复后专项：`python3.13 -m pytest -q tests/test_impl_fix1_regression.py tests/test_impl_fix4_blocker_regression.py tests/test_dispatch_fuse.py`，exit 0，46 passed。
- 全量与循环门禁：`~/HAFlow/bin/herdr-loop eval`，score 100.0，1420/1420 tests，lint 2760（baseline 2844，new 0）。
- 关联实现：`herdr/state_db.py`、`herdr/liveness.py`、`services/herdr-controller.py`、`herdr/delivery_record.py`、`herdr/workflow_docs.py`、`herdr/agent_router.py`、`bin/herdr-task`。

### 相关文档 / 关联证据

- 共享 spec/旧修复说明：`wf-haflow-0924-01/shared/notes.jsonl` 的 `FR-spec草稿`、`req-spec需求规格`、`test-0924测试报告`、`review-0924独立评审报告`、`impl-fix1修复说明`。
- 回归入口：`tests/test_impl_fix4_blocker_regression.py`、`tests/test_impl_fix1_regression.py`。

---

## 91. 测试进程会写穿实盘注册表：JSON 投影的回落地不能是用户 HOME

### 问题背景

`wf-haflow-0924-01` 收尾节点在 clone 内执行标准验收命令 `pytest -q` 后，操作者的实盘任务
注册表 `~/.herdr-controller/tasks.json` 被整体覆写为单条测试 fixture 记录（313 → 1）。
权威库 `state.db` 未被破坏（313 条完好），受损的只有 JSON 投影——但投影正是人工排查与
`herdr-task` 部分读取路径的入口，操作者视角就是"我的任务账本没了"。

最小复现（该用例本身与门禁无关，1 passed，副作用才是问题）：

```bash
pytest -q "tests/test_trajectory.py::test_normal_launch_persists_one_new_run_id_for_initial_trajectory_events"
```

### 根因

审计钩子捕获的真实调用栈（`sys.addaudithook` 监听 `os.replace`）：

```
tests/test_trajectory.py:361
 → bin/herdr-task:2343 _launch_task
 → bin/herdr-task:1151 save_tasks                    # 此处 tasks_file=None
 → herdr/state_store.py:107 sync_tasks_projection(store=store, tasks_file=None)
 → herdr/state_store.py:49  _sync_projection_locked
 → herdr/state_store.py:37  _atomic_write_json → os.replace(tmp, ~/.herdr-controller/tasks.json)
```

用例已用 `monkeypatch.setenv("HERDR_STATE_DB", tmp)` 隔离了数据库，但没有隔离投影目标：
`tasks_file=None` 让 `resolve_tasks_projection_file()` 逐级回落（显式参数 → `TASKS_FILE`
→ `store.db_path.parent` → `state_db.CONTROLLER_DIR`），最终落回实盘
`~/.herdr-controller/tasks.json`。`tests/conftest.py` 只隔离了 `HERDR_WORKFLOW_DOCS_DIR`，
未覆盖注册表投影。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 隔离了 DB 就以为隔离了整个状态 | 「权威库 SQLite」与「JSON 投影」是两条独立写路径，隔离前者不保证后者 | 测试凡间接触碰 `sync_*_projection`，必须让投影跟随已隔离的 store（见下「修复方案实测」——**不要**用 conftest 钉 `TASKS_FILE`/`WORKFLOWS_FILE`） |
| 回落链末端是用户 HOME | 回落链最后一级指向实盘目录时，任何"忘了传参"都会静默写穿 | `resolve_*_projection_file` 在"已设 `HERDR_STATE_DB` 却未设 `TASKS_FILE`"时应拒绝或强制跟随 store，不回落到 HOME |
| 写入异常被吞 | `_sync_projection_locked` 的 `except Exception: pass` 让失败与成功同样不可观测 | 投影写入失败必须留可观测事件或告警，不能静默 |
| 排查时信 stderr | pytest 会捕获 `sys.stderr`，写在其中的诊断输出在用例通过时被丢弃 | 诊断钩子必须写独立文件，不要写 stderr |
| 只靠"文件没变"下结论 | 拿 `chflags uchg` 让目标文件不可变，才能把"谁在写"从推测变成证据 | 存疑时用不可变/只读对照实验做反证 |

### 验证命令 / 关联证据

- 覆写复现：先 `sync_tasks_projection()` 复原到 313，再跑上面那条用例，之后
  `python3 -c "import json;print(len(json.load(open('$HOME/.herdr-controller/tasks.json'))['tasks']))"`
  → `1`（预期 313）。
- 反证（证明写入目标就是该文件）：`chflags uchg ~/.herdr-controller/tasks.json` 后重跑同一用例，
  注册表保持 313 且用例仍 `1 passed` —— 说明写入目标确为实盘路径，且失败被静默吞掉。
- 权威栈追踪：`sys.addaudithook` 记录 `os.replace(src, dst)`，输出写文件后得到上文调用链。
- 恢复：`python3 -c "from herdr.state_store import sync_tasks_projection; sync_tasks_projection()"`
  （从 `state.db` 重投影，313 条复原；`state.db` 全程未受损）。
- 归因：`git diff 437b335..34bfd30 -- bin/herdr-task herdr/state_store.py tests/test_trajectory.py`
  → 调用点（`save_tasks` 的 `sync_tasks_projection`）与回落逻辑在 base 上已存在，
  `tests/test_trajectory.py` 本分支零改动，**存量缺陷，非本次交付引入**。

### 修复方案实测：不要用 conftest 钉 `TASKS_FILE` / `WORKFLOWS_FILE`

上面初稿的第一条"规范"（在 `tests/conftest.py` 全局钉住 `TASKS_FILE` / `WORKFLOWS_FILE` 指向 tmp）
**已被实测证伪**，请勿照做：

```bash
# ① 钉 env 跑全量：注册表安全，但打破 3 个用例
TASKS_FILE=/tmp/x/tasks.json WORKFLOWS_FILE=/tmp/x/workflows.json pytest -q
# → 3 failed, 1462 passed, 44 subtests passed
#    FAILED tests/test_fix_loop_gates.py::SetVerdictTest::test_same_status_completed_still_persists_verdict
#    FAILED tests/test_fix_loop_pr1.py::SuppressAutoCloseLatchTest::test_controller_skips_auto_close_while_latched
#    FAILED tests/test_fix_loop_pr1.py::SuppressAutoCloseLatchTest::test_first_active_task_clears_latch
#    两次实测注册表均保持 313 条（该 env 确实挡住了写穿，代价是打破用例）
# ② 不钉 env 跑全量：3 个用例恢复
pytest -q  # → 1465 passed, 44 subtests passed
```

为什么会打破用例：本仓库测试的隔离风格是改写**模块全局**（`_ht.TASKS_FILE` / `_ctl.WORKFLOWS_FILE`），
而 `herdr/kernel.py::_get_store()` 是**直接读 `os.environ`**（`kernel.py:64` `tasks_file = os.environ.get("TASKS_FILE")`、
`kernel.py:70` 同理 `WORKFLOWS_FILE`），并且在命中时按 `p.parent / "state.db"` **另开一个权威库**
（`kernel.py:66-73`）。于是全局钉 env 之后，这些用例的写入落到被钉的库、而断言读的是自己那份 tmp JSON，
`set_status` / 清 latch 的结果不复现——`globals()` 优先的 `herdr/agent_router.py:132-150` 不受影响，
唯独 `_get_store()` 这条直读 env 的路径被劫持。

**结论**：`TASKS_FILE` / `WORKFLOWS_FILE` 在本仓库不只是"投影路径"，它同时是**权威库的选址开关**。
修这个缺陷要走**收窄回落链**（让投影跟随已隔离的 store），而不是全局改环境变量：

1. `resolve_tasks_projection_file()` / `_sync_projection_locked`：已设 `HERDR_STATE_DB` 时投影必须跟随
   该 store，禁止回落到 `state_db.CONTROLLER_DIR`（治本，且不触碰 store 选址）；
2. `_sync_projection_locked` 的 `except Exception: pass` 改为可观测（否则改错了也看不出来）；
3. 若将来确实要钉 env，必须**同时**钉 `HERDR_STATE_DB` 到同一目录，否则 `_get_store()` 会另开库
   （`kernel.py:66-73`）——这是本条的实测教训，也是判断"能否用 env 兜底"的判据。

### 相关文档 / 关联证据

- `herdr/state_store.py#resolve_tasks_projection_file` `#sync_tasks_projection` `#_sync_projection_locked`
- `herdr/kernel.py#_get_store`（直读 env 并另开库，是"钉 env"方案证伪的关键）
- `bin/herdr-task#save_tasks` `#_launch_task`
- `tests/conftest.py`（仅 `HERDR_WORKFLOW_DOCS_DIR` 隔离）
- 同类模式（同一"默认路径回落到生产状态库"根因，本条是 JSON 投影侧的实例）：
  §78（`store=None` 回落到生产 `state.db`，已用 conftest kill switch 收口——但该 kill switch
  只覆盖 observer，覆盖不到本条的投影回落，且**不能**靠钉 `TASKS_FILE`/`WORKFLOWS_FILE` 补，见上节实测）、
  §89（收尾节点分支 ≠ 交付物分支：收尾侧必须对"看似无关"的实盘副作用保持警惕）
