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
| **PID 变了就认为新代码已加载** | PID 变化只证明进程重启。console/controller 的 plist 指向 `releases/<commit>` 冻结快照，且 `kickstart` **不重读 plist**——改完配置后 `kickstart` 起的仍是旧路径 | 部署后必须核对运行中进程的**实际加载路径**（`ps -o command= -p <pid>`）并与 `git rev-parse HEAD` 比对；改了 plist 一律 `bootout`+`bootstrap`。详见 **§108** |

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
| **多环境常驻进程分发落后** | 部署于用户主目录或系统级 LaunchAgents 的服务脱离 Git 工作区，直接改动仓库代码不会自动热加载 | 服务脚本修改后必须前置重新分发并让服务加载新代码。**当前拓扑已变**：console/controller 跑 `releases/<commit>` 冻结快照（`install-herdr-console.sh` 部署的副本不生效），sentinel/notifier 直跑工作区——改前先确认"该服务实际跑哪份代码"，流程见 **§108** |
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
3. 涉及 `console/` 任何改动，必须重新分发并让服务真正加载新代码，否则线上仍是旧逻辑。**注意：部署方式已变更** —— `com.user.herdr-factory-console` 与 `com.user.herdr-controller` 的 plist 指向 `~/.herdr-controller/releases/<commit>` 冻结快照，`./scripts/install-herdr-console.sh` 部署到 `~/.herdr-console/` 的副本**不会被执行**。正确流程（提交 → 重建快照 → 改 plist → `bootout`+`bootstrap`）见 **§108**；改 plist 后仅 `kickstart` 无效；
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
| **Pane 数量失控** | 开放式并行提示词会导致 LLM 倾向于无限切碎工位，带来巨大的 UI 与协调开销 | 不能把旧 `max_agents` 模式开关当作配额；分别设置 `max_concurrency` 与 `max_tasks_per_node` 并在 launch 硬校验 |
| **单视角思维盲区** | 需求与计划若无专门的对抗角色，漏洞往往要流转到下游甚至线上才暴露 | 需求与计划阶段强制收敛为**严格双工位**（`max_concurrency: 2`）：1 个主执行者 + 1 个对抗性质询者，两份互补交付物完备后方可通过门禁 |
| **强耦合代码并发冲突** | 任务并发必须建立在“文件集合完全解耦”的前提下，强耦合代码并发只会制造合并灾难 | 实现阶段采用**自适应并发**（上限 3）：解耦任务并发，强耦合或单点改动强制单 Agent 顺序执行 |
| **裁判与运动员同体** | 同一 Agent 往往具备相同的认知盲点，无法有效指出自身代码的隐性架构缺陷 | 测试、评审与收尾阶段强制**跨阶段硬隔离**（`exclude_stage_agents: [implementation]`），调度器自动剔除实现者，由跨模型独立把关 |

### 操作规范

1. **工位上限与双工位规范**：`software-development-v1.yaml` 的需求与计划阶段设置 `max_concurrency: 2`，声明 `roles: [executor, challenger]`，分别输出核心规格与《对抗审查与边界漏洞清单》；
2. **规则化角色直接派发**：`herdr/direct_dispatch.py` 支持解析 `roles`，常规推进直接生成双工位 Task 规格，免除协调器回合等待；
3. **调度器跨阶段硬隔离**：`herdr/agent_router.py` 的 `choose_agent` 解析 `exclude_stage_agents` 策略，自动查询并剔除对应阶段已分配的 Agent，且在单 Agent 受限环境下提供优雅降级保护；
4. **单工位独立验收**：测试、评审与收尾阶段严格限制为单工位（`max_concurrency: 1`, `parallel: false`），杜绝 Pane 泛滥。

### 2026-10-02 根因纠正

- 问题背景：计划节点声明 `max_agents: 2` 仍能累计派发多个动态工位，归档 superseded/failed 后留下引用。
- 经验教训：`direct_dispatch.classify_dispatch` 只比较该字段是否为 1；数值外观没有配额实现。Task 状态归档也不代表实例资源已释放。
- 操作规范：拆分并发与累计预算；旧配置显式确认审计；公开每节点引用计数。默认原位 rework，例外替换必须说明原因并计数。归档事务标 orphan，reap 以实例身份而非 pane_id 单独判定所有权。
- 验证证据：节点配额测试覆盖跨进程竞争与真实 CLI/StateStore；生命周期测试覆盖失败重试、实例不明保留与同 Task 返工。生产 7 个工位尚无 workflow ID，本轮不据猜测回收。

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
# → 3 failed, 1462 passed, 44 subtests passed（本轮实测）
#    FAILED tests/test_fix_loop_gates.py::SetVerdictTest::test_same_status_completed_still_persists_verdict
#    FAILED tests/test_fix_loop_pr1.py::SuppressAutoCloseLatchTest::test_controller_skips_auto_close_while_latched
#    FAILED tests/test_fix_loop_pr1.py::SuppressAutoCloseLatchTest::test_first_active_task_clears_latch
#    注册表保持 313 条（该 env 确实挡住了写穿，代价是打破用例）
# ② 不钉 env 跑全量：3 个用例恢复
pytest -q  # → 1465 passed, 44 subtests passed
```

**失败条数随"被重定向的库是否干净"变化，不要把它当作固定值**（独立审阅者用全新 `mktemp -d`
只跑那 3 条用例时得到的是 `2 failed, 1 passed`，与上面的 `3 failed` 都对，条件不同）：

```bash
T3=(tests/test_fix_loop_gates.py::SetVerdictTest::test_same_status_completed_still_persists_verdict \
    tests/test_fix_loop_pr1.py::SuppressAutoCloseLatchTest::test_controller_skips_auto_close_while_latched \
    tests/test_fix_loop_pr1.py::SuppressAutoCloseLatchTest::test_first_active_task_clears_latch)
B=$(mktemp -d); TASKS_FILE=$B/tasks.json WORKFLOWS_FILE=$B/workflows.json pytest -q "${T3[@]}"
# → 2 failed, 1 passed（test_controller_skips_auto_close_while_latched 通过）
# 复用上一轮跑过的同一份 /tmp 目录（库中已有状态）再跑：
TASKS_FILE=/tmp/x/tasks.json WORKFLOWS_FILE=/tmp/x/workflows.json pytest -q "${T3[@]}"
# → 3 failed
```

统一解释：**被重定向的那个库里已有的内容会串味**。全量运行时是同一进程内其它用例先写进去了，
单跑时则是上一轮跑剩的——所以 `2 failed` 与 `3 failed` 是同一机制在两个"脏度"下的表现，
指向的结论相同。**判定该缺陷是否被 env 钉住，用全量跑 + 校验注册表条数（313）即可，不要拿固定失败数当判据。**

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

### 2026-09-30 复发补证：调用者默认值越过所选数据库

**问题背景**：`wf-project-0929-01` 排查再次发现实盘投影仅剩测试Task，SQLite业务行仍在。当前resolver已有`store.db_path.parent`回落，但CLI把默认宿主路径作为显式参数传入，绕过resolver；steering和默认全量导出也有同类宿主默认值。

**经验教训**：修复回落层不等于调用链收口。默认配置路径不能冒充显式选库或显式投影覆盖；opt-in迁移的隐式输入也须属于所选库命名空间。真正显式指定的环境/模块/调用参数路径继续有效，不替用户改写。

**操作规范与防护**：`bin/herdr-task`仅保留显式投影覆盖，默认`workflow.json`仍用于配置读取；steering复用StateStore投影解析，SQLite默认导出/迁移使用该实例父目录。`tests/test_state_projection_namespace.py`用两个临时命名空间检查宿主字节不变、隔离库真实写入/回读、显式覆盖有效，以及空库不导入宿主Task。不得仅在conftest全局钉一组env掩盖产品缺陷。

**验证与证据**：同一回归修前及撤销关键修复均`5 failed,2 passed`，修后`7 passed`；相邻专项`59 passed`，隔离全量`2613 passed,145 subtests passed`（355.47s）。`python3.13 -m pytest -q tests/test_state_projection_namespace.py`安全使用临时路径；证据见本轮执行计划C26与该测试。仅本地验证，未重建实盘JSON、未部署；旧章节中的313条为历史快照，不能当作当前数量。

### 2026-10-01 复核补证：缺失文件不是未选择路径

**问题背景**：C26复核用空selected/workflows.json触发迁移，selected缺tasks.json时，调用者传None；底层迁移回退host/tasks.json，将宿主Task导入隔离SQLite。workflows/steering同因；缺checkpoint还会读取宿主文件，即使外键拒绝落库也已越过读取边界。

**经验教训**：路径解析与文件存在性属于不同决策。不存在的已选路径不能被转换成“使用默认值”；只测全套文件都存在或全部都不存在会漏掉配套输入部分缺失。

**操作规范与防护**：SQLiteStateStore传递全部已解析companion路径，迁移reader自行跳过缺失文件；保留明确指定外部源的契约。TEMP四类分别检查宿主未读取、字节不变、隔离库不含宿主对象；显式迁移作正常对照。

**验证与证据**：修前及撤销实现均4 failed/8 passed，修后12靶向与64相邻通过；全量2687 passed/145 subtests passed（378.99s），独立复审无此项阻断。未部署、未修实盘历史投影。证据为执行计划C26b与test_state_projection_namespace.py。

## 92. SQLite `mode=ro` 并非无副作用：WAL 缺边车时打开会实体化 `-wal`/`-shm`

### 问题背景

PR #102 把 shadow-eval 切到只读连接（`mode=ro` + `query_only`），测试证明"主库字节不变、
零业务写"后仍被评审揪出 P1：对一个 WAL-mode 数据库（所有连接关闭后 last-close checkpoint
已删除边车，文件头仍是 WAL），只要父目录可写，`sqlite3.connect("file:...?mode=ro", uri=True)`
会由 SQLite **创建** `-wal`/`-shm`——一个只读诊断工具在盘上留下了文件。

### 根因

`mode=ro` 只约束主库可写性；读 WAL 库仍需 WAL 索引（`-shm`）。按 sqlite.org/wal.html
"read-only WAL"，SQLite 仅在 `-shm` **无法以读写方式打开**（目录不可写）时才走堆内存
模拟路径；目录可写时优先实体化真实文件。此前"没留下文件"的用例是**假阳性**：fixture 在
run 前用 `mode=ro` 连接做快照，快照自己先把边车建了出来，掩盖了被测进程的行为。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| `mode=ro` = 不写主库 = 无副作用 | 主库不变 ≠ 不落任何文件；WAL 读取必须先有边车对 | 只读入口打开前用原始字节读文件头 18/19 判定 WAL（不经 SQLite，零副作用），WAL 且边车缺失即 `ReadonlyWalSidecarError` fail-closed，绝不让 SQLite 建文件 |
| `immutable=1` 能绕过 | 它跳过 WAL/shm 检查，但生产 `state.db` 随时可能被并发提交，会读到过期/撕裂快照 | 对活库禁用 immutable；fail-closed + 边车在场才是正确取舍 |
| 快照辅助连接自己开库 | 快照污染被测现场，制造假阳性 | run 前快照用原始字节 + 目录集合 before/after 全量比对；能不开 SQLite 就不开 |
| quiescent WAL 库被拒读是否过严 | 边车缺失 ⟺ 无存活连接；HAFlow 诊断的真实场景与 controller/worker 并发、边车在场 | 宁可拒读不脏盘：CLI exit 1，消息点明 "refusing to create -wal/-shm" |
| fixture 天然 quiescent | store 每次调用独立开关连接，最后一次 close 把边车 checkpoint 掉，进程内只读用例全部被 fail-closed 拒绝 | `_make_env` 保一条打开的 WAL 连接（keeper，无数据写）维持 live 态；专测缺失态的用例先显式关 keeper 再断言 |

### 验证命令 / 关联证据

- 复现：`init_db` 后关闭全部连接 → 目录无 `-wal`/`-shm`、文件头 18/19 = 2/2 →
  再开一个 `mode=ro` URI 连接 → 目录出现两个边车文件（本仓库实测）。
- 回归：`pytest -q tests/test_shadow_evaluation.py` → 47 passed；其中 case3b 断言
  fail-closed（exit 1、stderr 含 `no existing read-only sidecars`、目录文件集合逐字不变），
  case3c 断言边车在场时读取成功且主库字节一致。
- 全量：`pytest -q` → 1799 passed + 44 subtests。
- 关联：`herdr/state_db.py#get_readonly_db_connection` `_is_wal_mode_database`
  `_has_readable_wal_sidecars`；sqlite.org/wal.html#readonly；§91 同族
  （测试 fixture 的隐式文件系统副作用）。

### 修正（2026-09-27, PR #102 评审）：撤回 fail-closed preflight，边界重划为「HAFlow 持久状态只读」

本教训引入的 `ReadonlyWalSidecarError` preflight（含 `_is_wal_mode_database` /
`_has_readable_wal_sidecars` / fixture keeper / case3b–case3c）经评审撤回，上表
「宁可拒读不脏盘」「fixture 保 keeper」两行以本修正为准：

- **TOCTOU 不可解**：「检查边车缺失 → 再打开」非原子；并发进程恰可在窗口内创建或删除
  边车，pre-flight 结论在执行时可能已过期，不构成 race-free 的保证，只是把竞态换成
  偶发拒读。
- **边界划错**：`-wal`/`-shm` 是 SQLite 自己的连接协调文件，属 OS/数据库协调层，不是
  HAFlow 持久数据。只读契约应定义为「不改 HAFlow 的应用数据与 schema」——`mode=ro` +
  `query_only` 已挡住应用层写入与建库；「该目录一个文件都不许出现」over-scope 了。
- **利弊失衡**：容忍边车不削弱任何真实不变量（不建库、无 DDL/migration、无业务写、
  journal_mode 不翻转，全部保留并有测试覆盖）；fail-closed 反而把「静置库但 WAL 头」
  的合法读取（边车已被最后一次 close checkpoint 清掉，库内容完整可读）也拒掉。

核心结论仍然成立且不受本修正影响：`get_db_connection` 有建库/迁移副作用，只读入口必须
独立于它；fixture 快照自开 SQLite 连接会污染被测现场、制造假阳性。

---

## 93. 身份必须作为不可变执行身份贯穿全链：claim / launch / completion 三者不可互相顶替

### 问题背景

PR #107（Critical-Path Scheduler v1）在五轮评审中反复暴露同一类缺陷：调度器决定
了「要验证哪个候选」，但这个身份只在**派发那一刻**被绑定，后续任何一环丢失或降级
都会让门禁「证明了一件没有证明的事」。五轮共 11 项，全部是身份链断裂，没有一项
是功能缺失：

| 轮次 | 缺陷 | 后果 |
|---|---|---|
| 3 | Coordinator 回落丢 frozen SHA/branch | 同一 Scheduler decision 出现两套执行语义：直派验证 A，回落让总指挥自己重猜 |
| 3 | 证据只有启动时的 `baseline_commit` | Agent 执行期间 `git pull` 后，任务仍声称验证 A，实际验证的是 B |
| 3 | A→B→A 冻结与全历史比对 | 回到 A 时被当成 noop，`latest` 停在 B，之后 Test/Review 全部卡死 |
| 4 | 取证 best-effort + 取证失败仍接受 pass | completion evidence 缺失时 fallback 回 launch evidence，等于撤销第 3 轮的修复 |
| 4 | `rev-parse` 失败后退回前缀匹配 | Git 明确说「无法唯一解析」的场景被当作同一个 commit 放行 |
| 5 | frozen lookup 异常 → `engaged=False` | 一次 SQLite 错误把 scheduler 管理的 workflow 重分类为 legacy，wrapup 绕过门禁 |
| 5 | freeze 返回空仍占 stage latch | 无身份派发被 preflight 拒绝，而 stage 已锁，天然重试被抑制 |
| 5 | 回收用裸 `tmux kill-pane` | 不检查 return code 就打印成功；且销毁借用的 prebuilt Pane |

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 身份只在 dispatch 绑定 | 同一个决策只能有一套执行语义，回落路径必须**原样透传**，不得重新推断 | 回落读 `candidate_frozen` 事实本身（不是重新 resolve），透传 `--candidate-sha` / `--onto` |
| 用启动快照当完成证据 | `baseline_commit` 是 launch 证据，`verified_candidate_sha` 才是 completion 证据 | 提交 `pass/blocked` 时重新读 clone HEAD；`baseline_commit` 不得被称作 verified SHA |
| 证据获取 best-effort 后还能降级 | 强证据一旦缺失就退到弱证据，等于没有强证据 | 取证失败即 fail-closed：无 `verified_candidate_sha` 的 scheduler-managed 任务拒绝 verdict |
| 幂等按全集比对 | 候选身份是 **episode** 不是集合，回到历史值是真实轮换 | 冻结幂等只与 **latest** 比对，`A→B→A` 必须写第三条 |
| 异常被当成「不存在」 | 「查询失败」与「查询成功但为空」必须三态区分 | lookup 异常 / store 缺失一律 fail-closed；仅「成功且为空」才 legacy passthrough |
| 门禁前置失败仍推进阶段 | 门禁在 latch 之前判定，失败就不得占闩 | 无 frozen candidate → 不 latch、不派发，下轮 sweep 重估 |
| 回收绕过自己的后端 | Herdr Pane 不等于裸 tmux pane；且共享资源有归属 | 只关闭本进程自建的 dynamic Pane（走 `close_pane`）；prebuilt Pane 只释放占用 |

### 操作规范

- **身份三字段不可互相顶替**：`candidate_sha`（要求验证谁）/ `baseline_commit`（启动时
  clone 在谁）/ `verified_candidate_sha`（完成验证时实际验证了谁）。门禁只认第三个。
- **fail-closed 必须区分「不知道」和「没有」**：任何把异常折叠成空值的 `except` 都要复查，
  那是把 fail-closed 变回 fail-open 的最短路径。
- **强证据的 fallback 链要逐环审计**：新增 fallback 前先问「上一环缺失时，我是不是在
  用更弱的证据冒充同一件事」。
- **fail-closed 需要作用域**：无候选声明的任务不构成任何身份断言，一律拒绝会打断无关
  workflow；无 `project_root` 的 workflow 永久解析不出候选，拦下即死锁。

### 验证命令 / 关联证据

- 修复后全量：`python3.13 -m pytest -q` → 2033 passed + 50 subtests。
- 专项回归：`tests/test_scheduler_v1.py`、`tests/test_scheduler_facts.py`、
  `tests/test_scheduler_dispatch_e2e.py`（含 claim-only 放行、ABA 轮换、回落透传、
  ambiguous SHA、reclaim 归属、latch 顺序）。
- **变异验证**：逐项回退 11 处修复中每一处，均命中对应回归测试（单轮回退分别产生
  1/4/6/11 个失败），证明测试真的在守门而非恒真。
- 合并：`1ce6bdf`（PR #107，head `3ef0304`）。
- 关联实现：`herdr/scheduler.py`、`herdr/scheduler_facts.py`、
  `services/herdr-controller.py`、`bin/herdr-task`。

### 相关文档 / 关联证据

- 走查：`docs/walkthroughs/20260928-pr108-selective-reverification.md`
- 既有同族教训：§90（fix-loop 证据门禁与候选身份）、§88（隔离域不能用自身执行身份）。

## 94. 复用是最弱的一环：能被证明的只有「已声明」，策略身份必须是指纹不是版本号

### 问题背景

PR #108（Selective Reverification v1）让候选轮换时可以跳过重复验证。它引入了本仓库
第一条**主动放弃验证**的路径，因此每一处 fail-open 都比以往代价更高。三轮独立对抗
评审共 17 项，其中最要命的一项不是「新代码写错了」，而是**修复方式本身换了一个问题
而不是解决原问题**：

| 轮次 | 缺陷 | 后果 |
|---|---|---|
| 1 | 复用来源走 `extract_task_verified_sha` | 该函数为兼容 pre-Scheduler 会回退 `baseline_commit`，于是「启动快照」被当成「验证过 A」；#107 专门消灭的说法在这里复活 |
| 1 | 排除 superseded 来源 | fix-loop 返工必然作废上一轮 test 任务，于是复用来源永远不存在，功能在真实返工下永不生效 |
| 1 | 冻结在 ready 循环内 | 轮换对本轮 sweep 不可见，被旧事实满足的节点本轮仍算完成 → C 候选的 verifier 从未运行就被跳过 |
| 1 | 冻结上提后 `if deferred: return` | 该 return 早于 `is_workflow_completed` → 全部完成的 workflow 永远关不掉，每 2s 刷一次日志 |
| 2 | 用 `policy_version` 撤销复用 | 版本号是标签，收窄范围不改它 → 收窄策略纯属装饰；**而代码注释恰好宣称自己防住了这件事** |
| 2 | `is_node_complete` 读 `scheduler_core.EFFECTIVE_REUSE` | 可选组件缺失从「退回原语义」升级成热路径 AttributeError |
| 2 | 「重复事实 byte-identical 所以无害」 | `created_at` 逐次写不同，理由是假的；S5 工件照抄了这个说法 |
| 3 | 记忆化后又引入「先读后写」 | episode 检查先建 memo，plan 后写事实 → 同轮 readiness 读不到刚写的复用，静默重跑 |

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 复用会跳过验证 | 复用路径上**任何**宽松都是安全漏洞，不是效率问题 | 判据写成机器事实：git diff + 显式非影响范围 + 带 `verified_candidate_sha` 的 PASS + 不可变派生事实，四者缺一即 RERUN |
| 复用来源用 launch 证据 | §93 的「三字段不可顶替」对**新**代码同样生效，调用方选了宽 fallback 不代表要求变了 | 复用来源读字面字段，不复用为兼容旧流程设计的 fallback |
| 作废 ≠ 抹除 | superseded 只表示「不再是当前结论」，记录「验证过什么」必须留存 | 复用来源保留 superseded 记录，靠 `verified_candidate_sha == from_sha` 精确绑定取用 |
| 版本号当策略身份 | 人写的标签不会随配置变 | 策略身份 = **已解析策略的指纹**（版本 + 每个 verifier 的范围）；收窄范围和删配置都必须真正撤销既有复用 |
| 上提顺序改变可观测性 | 动作必须早于它要影响的计算 | 候选冻结上提到 readiness 之前；但「无候选」只能跳过待派发节点，**不得**早于 `is_workflow_completed` |
| 记忆化改变读写顺序 | memo 一旦先于写入建立，本轮就读不到刚写的事实 | 复位点放在写入**之后**；memo 覆盖策略上下文与事实，避免每节点重解析 YAML |
| 同一不变量两份实现 | 账本说「满足」而门禁说「不满足」，workflow 永久等待 | 台账与门禁共用同一个纯函数 `resolve_effective_verification`；死掉的第三份实现删掉 |
| 缺字段默认「好」 | `source.get("source") or "fresh"` 是 fail-*open* 默认值 | 缺 provenance 视为 unknown → RERUN |
| 验证工件自己造假 | 计数、不可复现的日志、错误的因果说明 | 数字用 `pytest --collect-only` 核对；日志逐行实跑复制；无法自行复现的指标必须署名归属 |
| 外部评审：reuse 事实只绑 SHA（第 4 轮才发现） | **回滚会重新冻结一个曾经冻结过的 SHA**；只认 SHA 时旧轮次的 reuse 复活，一个从未验证过的候选被判为已覆盖 | 事实绑定**冻结事件 id（episode）**而非候选 SHA；`A→B` 与后续轮次的 `A→B` 是两条 episode。测试必须覆盖「回到完全相同的 SHA」 |
| 外部评审：复用来源只查 evidence（第 4 轮才发现） | claim/evidence 不一致的 Task 连自己那一轮的门禁都过不了，凭什么替下一轮作证 | `source.candidate_sha` 与 `source.verified_candidate_sha` **必须同时**绑定 from 候选；新增 `source_candidate_claim_mismatch` |
| 并发写事实靠 read-then-compare | 8 个并发写者产生 8 行重复，append-only 审计账本出现重复事实 | 复用**已有的** `BEGIN IMMEDIATE` 写锁做 check+insert 原子化（与 interventions / collaboration_events 同一手法），不新建表、不新建锁系统 |

### 操作规范

- **复用的事实必须自解释**：`reusable_scope` / `out_of_scope_paths` / `policy_identity`
  与决策同批落盘，否则事后无法回答「凭什么判它安全」。
- **read-only 命令的守卫要打在 store 之前**：`get_state_store` 会跑 `init_db`（DDL +
  迁移），一个「只读」命令在空环境上就能造出 500KB 数据库。
- **测试隔离要改模块常量，不是环境变量**：`STAGE_STATE_FILE` 在 import 期被读成模块常量，
  `monkeypatch.setenv` 完全无效——实测曾把测试 workflow 写进用户真实
  `~/.herdr-controller/stage-state.json`。
- **不设基线的健康检查要显式豁免**，别伪装成跑过。

### 验证命令 / 关联证据

- 修复后全量：`pytest -q` → **2196 passed + 50 subtests**。
- 专项：`tests/test_reverification_v1.py`（118）、`test_reverification_core.py`（19）、
  `test_reverification_controller.py`（16）、`test_reverification_cli.py`（10）。
- 真实链路：临时 git 仓库 + 真实 `check_workflow_stage_advance` sweep，docs-only 轮换下
  **不产生 test launch**、只派 review，Join Gate 以 reuse 事实放行。
- **#107 兼容性是实测的**：`evaluate_join_gate(reuse_facts=None)` 与 `3a84659` 实现做了
  20 万组随机差分，0 处判决不一致。
- 隔离校验：`~/.herdr-controller/stage-state.json` 全量测试前后 sha1 一致
  （`b1f02b4f063d1c34`），无 `wf-rever*` 残留键。
- 关联实现：`herdr/reverification.py`、`herdr/scheduler.py`、
  `herdr/scheduler_facts.py`、`services/herdr-controller.py`、`bin/herdr-task`。

### 相关文档 / 关联证据

- S5 证据与 S6 评审：已归档至
  `docs/walkthroughs/20260928-pr108-selective-reverification.md`
  （含两处被评审推翻后重写的错误陈述，是「工件也会造假」的实例）
- 既有同族教训：§93（身份三字段不可顶替）、§91（测试会写穿实盘注册表）、
  §92（只读不等于无副作用）。

---

## 95. 选择性返工：归因是**结论**不是**身份**，而「无法归因」必须能被显式写下

### 问题背景

PR #110（Selective Replan v1）让门禁 blocked 后的返工可以从「整个 implementation 阶段
重来」收窄到「只重做被 Verifier 点名的 Task 谱系」。它引入本仓库第一条**按结构化归因
改写工作流范围**的路径：系统第一次依据 Verifier 写下的一句话，决定哪些已完成的工作
要作废。因此 fail-open 的代价不再是「多跑一次验证」，而是**悄悄作废未被点名的工作**。

一轮独立对抗评审（Opus 5，read-only）给出 `NEEDS_FIXES`，9 项缺陷中 3 项 blocking。
把它们的成因剥掉表象后只剩两条：**同一件事有两个名字 / 同一不变量有两份实现**。

| 编号 | 缺陷 | 后果 |
|---|---|---|
| F1 | 目标按**策略声明的** `retry_node` 校验，作废却按**门禁解析出的** `retry_node` 执行 | 两者不一致时「保留」过滤器永不命中：未被点名的 Task 被一并作废，同时留下一条自称 selective 的事实 |
| F2 | 「等待补派」另写了一套 `superseded_by` 规则，与补派管线 `lineage_redispatch_candidates` 分歧 | 分歧状态是一个**没有出口的终态**：节点永久钉在未完成，工作流带着作废谱系收口 |
| F4 | 机制修好了，**通知没修**：selective 下总指挥仍收到全量返工的 `herdr-task launch` 骨架 | Controller 刚起 B-r2，总指挥又按同一份通知全量重做实现——本 PR 要消灭的失败模式换了一条通道回来 |
| F5 | `record_event_if_absent` 返回 `exists` 但 payload 读不回时，`isinstance(stored, dict)` 无 `else` | 静默把**本次重算**的 plan 提升为权威，等于用未经持久化确认的结论改写事实 |
| F6 | 拒绝分支打印的是**计划自身**的 reason | `identity_content_mismatch`（同 episode 换 targets）这类真正原因被掩盖 |
| F7 | `stage_verdict_affected_task_ids` 只在非空时写入，永不被清除 | 上一轮点名 B，本轮 Verifier 明确「无法归因」，B 仍被继续作废——幽灵归因 |

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 归因来源可以推断 | 「猜」与「证」在持久化事实里长得一样，事后无法区分 | 唯一来源是结构化字段；缺字段/空列表/ID 不存在/跨 workflow/跨节点/非当前谱系头/已 superseded/候选身份或版本不可证/结论读不出/事实写不进 —— 十种情况**全部**回退 legacy |
| fail-closed 写成「过滤掉非法项」 | 「三个 ID 里两个合法就只用那两个」= 用一条**自己都无法解释其完整性**的结论改写工作流 | Fail-Closed 的强形式是**全有或全无**：一个非法 → 整个 selective 决策拒绝 |
| 同一件事两个名字（F1） | `policy.retry_node` 与 `gate_cfg.retry_node` 是两个独立结论，一致是巧合 | 不一致即证明不了「校验节点 = 作废节点」→ 整体回退；不做「以谁为准」的猜测 |
| 同一不变量两份实现（F2） | 与 §94 同一条失效模式，换个位置复发：账本/门禁/管线三个说法里只要有一个不同，就有一个状态无人负责 | 等待谓词**复用补派管线同一个函数**；判据同源 ⇒ 窗口必然闭合 |
| 「机制修好」当成交付完成（F4） | 消息是**真实的重新进入通道**，不是描述 | 修机制必须同时修它的每一条通知；补投（`redeliver_pending_fix_loop`）也要保留同一上下文，不给病理留第二通道 |
| 「显式无法归因」没有表示法（F7） | 缺失字段与显式空列表语义不同（前者=旧数据/未写，后者=本轮主动放弃归因）；只在非空时写入等于**取消了后者的表达能力** | 每次写 verdict 一律**覆写**该字段（含显式 `[]`）；`pass` 结论同样清除——结论换了，归因不能活过它 |
| `exists` 当作「可用」（F5） | 持久化成功 ≠ 读得回来；`isinstance` 少了 `else` 就是一条静默的 fail-open | 读不回即回退 legacy，**绝不**用本次重算结果顶替权威 |
| 身份里塞结论 | 同 episode 换 targets 会撞 id | 身份刻意**不含** targets（`replan_id = SHA256(workflow, gate_task, gate_version, gate_candidate_sha, retry_node, policy_identity)`）→ 代价是撞 id，因此撞 id **必须拒绝**（`identity_content_mismatch`）而不是覆盖；重放同内容返回 `exists`，崩溃恢复天然幂等 |
| 「重开节点」被当成「全量重派」 | 每轮 sweep 清 stage-advance 会绕开 `notified` 闩 | 安全边界不在闩上而在**判据同源**：只要 AWAIT 为真，补派管线就只产出被点名谱系的 `-rN`；窗口一关直派转 `wait`（不唤醒总指挥） |
| 保留任务被「顺手」写一下 | 「保留」若允许更新 `updated_at`/`version`，latch 就会被保留任务的落定解除 | 保留 = **零写入**（断言级证明：比对 status/version/branch/metadata/updated_at 完整快照）；latch 要求**每个** target root 在 `latch_ts` 之后都有替代成员（AND 不是 ANY） |
| 修复轮声称修好 | 写了用例 ≠ 用例能红 | 6 个守卫型修复逐条**反向改写**跑守卫用例，6/6 变红；跑完 `diff` 确认工作区与变异前逐字节一致 |

### 操作规范

- **绝不部分接受**：`affected_task_ids` 的校验是全有或全无的函数，
  不要在任何调用点做「过滤出合法的那些」。
- **顺序是硬约束**：构建 plan → 持久化不可变事实 → 才允许作废。
  **没有持久化成功的 selective 事实，就没有 selective 作废**（不存在「先作废、后补事实」的窗口）。
- **「无法归因」要写得出来**：字段缺失与显式 `[]` 必须都能表达，且写 verdict 一律覆写。
- **legacy 等价要可断言，不要靠读代码**：`selective_target_task_ids=None`、
  无 `inventory_block`、targets 为空 三条边界各有一个「逐字节等价」用例。
- **通知是通道**：改动任何自动回流机制时，检查它是否还有别的方式通知人或总指挥。
- **评审工件必须署名来源**：本轮 Round 2 的独立复审因环境故障（子代理创建全线
  `400 Model is unavailable`）不可得，工件里如实标为 `claude (self, round-2 delta)`，
  并写明 Round 1 的独立性成立、Round 2 不算独立通过——**不可得的证据不得被写成已通过**。

### 验证命令 / 关联证据

- 修复后全量：`pytest -q` → **2320 passed + 50 subtests**（EXIT=0，457.05s）；
  改动前基线 2230 → 零回归。
- 专项：`tests/test_selective_replan_core.py`（65）、
  `tests/test_selective_replan_controller.py`（26）。
- 真实链路：`test_real_git_replacement_builds_on_preserved_work` 在临时 git 仓库里让
  A/C 落盘真实文件、B 被 supersede，再断言 replacement 的 `context_branch`
  **仍含 A/C 产出**；两个用例驱动**真实** `check_workflow_stage_advance` 覆盖 sweep 正反两向。
- 隔离校验：`~/.herdr-controller/stage-state.json` sha1 全量前后一致
  （`b1f02b4f063d1c34`）；实盘 `state.db` 扫 `wf-srp%` 命中 0 行。
- 变异验证：6/6 守卫用例在反向改写后变红。
- 关联实现：`herdr/selective_replan.py`、`herdr/fix_loop.py`、
  `herdr/direct_dispatch.py`、`herdr/scheduler_facts.py`、
  `services/herdr-controller.py`、`bin/herdr-task`、
  `workflow_templates/software-development-v1.yaml`。

### 相关文档 / 关联证据

- S5 证据与 S6 评审：`.omc/verify-<session>.md` / `.omc/review-<session>.md`；
  长期副本 `docs/walkthroughs/20260929-pr110-selective-replan.md`
- Wiki：`wiki/dag-workflow-engine.md` §6
- 既有同族教训：§94（同一不变量两份实现 / 复用必须被证明）、
  §61（补派按谱系去重）、§91（测试会写穿实盘注册表）、§93（身份三字段不可顶替）
## 97. 派生运行（Replay）与验收（Eval）的双向解耦与物化时机：身份确认前不落谱系、验证事实不代行验收裁决

### 问题背景

在 `wf-haflow-0923-01`（Eval + Replay V1）的实现收敛过程中，前期原型代码暴露了三类隐蔽但破坏系统不变量的 correctness 缺陷（P1-1、P1-2、P2-1）：

1. **验收事实与技术验证混淆（P1-1）**：`herdr/eval_engine.py` 在推导 `requirements_satisfied` 时，错误地将 `verification_passed` 作为后备或主判据，把"编译/测试脚本跑通"与"需求指标达成"画等号，导致无验收数据的运行被虚假判为通过，且无法区分技术通过但业务失败（或技术未跑但人工已签发）的情形。
2. **派生运行策略环境逃逸（P1-2）**：`herdr/replay_engine.py` 在重构或回放一个历史 Run 时，若源 Task/Workflow 没有显式冻结策略，直接降级读取当前运行进程的全局配置或 CLI 默认策略，导致当前环境的全局策略（例如 `HERDR_STAGE_POLICIES`）向历史回放静默泄漏，破坏了“默认使用 frozen snapshot”的隔离性。
3. **血统谱系过早落盘（P2-1）**：在启动派生任务时，先写 `ReplaySpec`（记录 parent_run_id 与 child_run_id）再执行 `herdr-task launch`。当 launch 因调度、进程崩溃或参数校验失败时，或者由于并发竞态导致生成的 Task 实际并未继承 `replay_of` 时，数据库中已留存不可逆的孤儿谱系，造成 lineage 虚假。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 需求验收（Requirements）借用技术验证（Verification） | 语义分层混乱：Verification 是技术证据，Requirements 是业务/阶段裁决 | `requirements_satisfied` 仅由权威验收事实（`acceptance_verdict`、`stage_verdict`）产生，无事实一律返回 `null`（保持三态），严禁反向猜测与跨层代行 |
| 历史回放时全局配置静默污染 | 派生运行必须自闭环，当前宿主环境的动态配置不得污染历史重放 | 建立严格的 5 级策略继承链（ReplaySpec 策略 → 源快照 → Task 冻结 → Workflow 冻结 → 私有 definition），无来源时 policy 显式为 `null` 并标明 `policy_source: unavailable` |
| 先写谱系再启动派生，失败留孤儿 | 副作用顺序颠倒：尚未确立真实身份即持久化关系 | **必须在 launch 成功且双向核验身份**（`run_id == replay_run_id` 且 `replay_of == source_run_id`）后才写入 `ReplaySpec`；launch 失败或身份不一致必须执行包含 Task/Workflow/事件/快照文件的完整级联原子补偿 |

### 操作规范

```bash
# 1) 验收推导无猜想、双向解耦验证（herdr/eval_engine.py）
# 验证：仅读取 acceptance_verdict 与 stage_verdict，无 LLM judge，无伪 score
python3 -m pytest tests/test_eval_engine.py -k "test_eval_requirements_satisfied"

# 2) 回放策略来源 5 级继承链与防泄漏验证（herdr/replay_engine.py）
# 注入全局环境策略，确认源无 policy 时输出 policy=null / policy_source=unavailable
HERDR_STAGE_POLICIES='{"mode":"GLOBAL-LEAK"}' python3 -m pytest tests/test_replay_engine.py -k "policy"

# 3) 谱系写入时机与原子补偿验证（P2-1）
# launch 失败或身份校验失败时，断言 ReplaySpec 行数为 0，且 Task/Workflow/Event 彻底级联清理
python3 -m pytest tests/test_replay_engine.py -k "compensation"
```

### 验证命令 / 证据

```bash
# 1) PR86 候选提交与改动清单（7 文件 +348 -53）
git show --stat cc5e9a0
# bin/herdr-task                   |  10 ++-
# docs/architecture/eval-replay.md |   8 +-
# herdr/eval_engine.py             |  12 ++-
# herdr/eval_store.py              |  13 ++++
# herdr/replay_engine.py           | 157 ++++++++++++++++++++++++++++++-------
# tests/test_eval_engine.py        |  38 +++++++--
# tests/test_replay_engine.py      | 163 ++++++++++++++++++++++++++++++++++++---

# 2) 全量测试回归与 lint 基线
pytest -q  # 1191 passed, 44 subtests
ruff check bin/herdr-task herdr/eval_engine.py herdr/ev## 96. 收尾不是终点：交付物身份必须固定，stranded 恢复与同名分支分叉只读研判

### 问题背景

`wf-haflow-0923-01`（Eval+Replay V1）在 2026-09-23 曾以 **ABANDONED** 收尾：fix-loop
6/3 耗尽、`test-auto-r6` blocked（候选停在 `3be4362`，缺 Eval/Replay/Compare/CLI），
`impl-fix4` 的 7 个文件实现 stranded 在 retained clone 里。该结论写进了
`wiki/log.md` 的 wrapup 条目与 shared note `n-1790146021488-7cbf`。

但同一天稍晚，工作流被仲裁恢复并**收敛**了：

1. stranded 工作被重新提交为独立可审计提交 `eaf2afe`（impl-fix1 遗产）、`f847886`
   （impl-fix4 遗产），并在 commit message 里写明 provenance（来源 task + 来源 clone）；
2. 两次合入 `origin/main`（`db2b213`、`f4e8f90`）；
3. `impl-fix5` 把三个 P1 语义收敛为 `7a6f2ae`（真 preflight+launch、默认 frozen 拒绝、
   Eval 只留四事实字段、Compare 只留 before/after）；
4. `test-auto-r7` 全量 1183/1183 pass，`review-auto-r2` 独立评审 **MERGE_READY**（阻断缺陷 0）。

于是"收尾已 ABANDONED"的终态结论，与"交付物已在 `7a6f2ae` 收敛待合入"的事实**同时成立**
——append-only 的日志里出现了一对看似矛盾的记录。

收尾 clone 里还有一处陷阱：本地同名分支 `agent/opencode/feat-wf-haflow-0923-01-impl-t1`
停在 `3de67c8`，与远端 `7a6f2ae` **分叉**（merge-base `3be4362`）：本地 4 个提交、远端 6 个。
按"本地落后就该对齐"的直觉去做 `reset --hard origin/...`、`pull` 或 `branch -D`，看似只是
收拾残局。**只读研判后的事实是**：本地那支是同一批工作在新 main（`dfa5e38`）上的重放，
其树 `722c95e1` 与 `git merge-tree --write-tree origin/main 7a6f2ae` 的结果树**逐字节相同**
——本地分支**没有任何独有内容**，它恰好就是「当前 main ⊕ PR86」的干净合并结果。
换句话说：分叉 ≠ 工作丢失，commit 身份不同 ≠ 内容不同；权威 head 仍由 PR86 指向的
`7a6f2ae` 决定。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 收尾条目只写终态结论，不固定交付物身份 | 结论会被后续进展合法推翻，append-only 日志无法自我对齐 | wrapup 条目必须同时写 **PR URL + head SHA + base**；后续条目用"取代/补充"表述，而非制造矛盾 |
| 作废被当成"工作消失" | 作废 ≠ 工作丢失，retained clone 是最后的事实来源 | 恢复以 clone 为源，**每次恢复单独成一个提交**，commit message 写明 provenance（来源 task/clone），让审计链可见 |
| 用"谁更新/谁提交多"判断权威 head | 权威 head 由 **PR 指向**决定，不由时间戳或 commit 数决定 | 同名分支分叉一律只读研判；禁止 reset/pull/delete，清理决策交 Controller |
| 把"分叉"直接当成"存在独有工作" | 分叉可能只是同一工作在新 base 上的**重放**：commit 身份不同而内容等价 | 用**内容等价性**判定而非数提交：`git merge-tree --write-tree <base> <head>` 的结果树 vs 本地树比对 |
| "零改动/无集成"的收尾结论 | 恢复收敛后该结论变成误导性证据 | 收尾报告必须给出候选 head + PR 状态 + 相对 base 的提交数，让下一位读者自行判断时效 |

### 操作规范

```bash
# 1) 固定交付物身份（收尾条目必写）
gh pr view 86 --json url,baseRefName,isDraft,state,headRefName
git rev-parse origin/agent/opencode/feat-wf-haflow-0923-01-impl-t1   # → 7a6f2ae

# 2) 进入合并确认前的两项前置（只读）
git rev-list --count origin/main..<head>        # 必须 > 0，否则会被 git cherry 误判为已合入
git show-ref --verify --quiet refs/heads/<branch>  # 分支不存在 → 报「已清理/无资源」，不得记为失败

# 3) 同名分支分叉只读研判
git merge-base <local> <remote>                # 分叉点
git rev-list --count origin/main..<local>       # 本地独有提交数
git cherry origin/main <local>                  # 非空 ⇒ 未合入

#al_store.py herdr/replay_engine.py  # 0 errors

# 3) 独立评审结论（review-auto-r3）
# MERGE_READY 十条条件全通过，阻断缺陷 0 项，7 项非阻塞建议汇总追踪
```

- 门禁证据（shared `notes.jsonl`）：
  - `impl-fix6`：`n-1790163114271-36f8`（P1-1 解耦、P1-2 策略继承链、P2-1 身份校验后写 spec，全量 1191/1191 绿）
  - `test-auto-r8`：`n-1790163892258-aee0`（clone detached@cc5e9a0 验证，1191/1191 pass，触改文件零新增 lint，Compare 四字段）
  - `review-auto-r3`：`n-1790165060831-9aed`（MERGE_READY 明确通过，阻断缺陷 0）

### 相关文档 / 关联证据

- `docs/architecture/eval-replay.md`（Eval Engine、Replay Engine 与 Compare 架构规范）
- `herdr/eval_engine.py`、`herdr/replay_engine.py`、`herdr/eval_store.py`
- `wiki/log.md`（fix6 轮收尾条目与二次收尾关系）
- PR: https://github.com/allinai0506/HAFlow/pull/86
 4) 内容等价性判定（决定本地分支是否真有独有工作，而不是数 commit）
test "$(git rev-parse <local>^{tree})" \
   = "$(git merge-tree --write-tree origin/main <remote-head> | head -1)" \
  && echo "本地无独有内容 = main ⊕ PR head"
# 同时得到合并指引所需的冲突预检：merge-tree 退出码 0 即无冲突

# 5) stranded 恢复：以 clone 为源、单独提交、写明 provenance，不 force-push
```

### 验证命令 / 证据

```bash
gh pr view 86 --json url,baseRefName,isDraft,state
# {"url":"https://github.com/allinai0506/HAFlow/pull/86","baseRefName":"main",
#  "isDraft":true,"state":"OPEN","headRefName":"agent/opencode/feat-wf-haflow-0923-01-impl-t1"}
git log --oneline -1 origin/agent/opencode/feat-wf-haflow-0923-01-impl-t1   # 7a6f2ae
git rev-list --count origin/main..7a6f2ae                                    # 6
git merge-base agent/opencode/feat-wf-haflow-0923-01-impl-t1 origin/agent/opencode/feat-wf-haflow-0923-01-impl-t1  # 3be4362
git rev-parse 3de67c8^{tree}                                                 # 722c95e1…
git merge-tree --write-tree origin/main 7a6f2ae | head -1                    # 722c95e1…（同上 ⇒ 本地无独有内容）
git merge-tree --write-tree origin/main 7a6f2ae >/dev/null; echo $?          # 0 ⇒ PR86 合入 main 无冲突
```

- 门禁证据（shared `notes.jsonl`）：`test 门禁结论 pass`（r7，1183/1183）、
  `review 门禁结论 pass`（review-auto-r2，MERGE_READY 十条件全 PASS / 阻断缺陷 0）。
- 首次收尾记录：`wiki/log.md` `[2026-09-23] wrapup | ... 收尾 abandon`；shared note
  `n-1790146021488-7cbf`。

### 相关文档 / 关联证据

- `workflow_templates/software-development-v1.yaml#wrapup`（六步收尾执行规则，收尾条目须含 PR URL）
- `wiki/log.md`（wrapup 条目；本文件 §86 的根因是 stranded 的**成因**，本节是 stranded 的**收敛与取证**）
- `.agents/skills/six-step-finish/SKILL.md`（步骤 0 交付 PR 前置；本技能严禁自动合并）
- 现场：`~/.herdr-controller/workflows/wf-haflow-0923-01/shared/notes.jsonl`、PR86
## 98. 派生运行（Replay）与验收（Eval）的双向解耦与物化时机：身份确认前不落谱系、验证事实不代行验收裁决

### 问题背景

在 `wf-haflow-0923-01`（Eval + Replay V1）的实现收敛过程中，前期原型代码暴露了三类隐蔽但破坏系统不变量的 correctness 缺陷（P1-1、P1-2、P2-1）：

1. **验收事实与技术验证混淆（P1-1）**：`herdr/eval_engine.py` 在推导 `requirements_satisfied` 时，错误地将 `verification_passed` 作为后备或主判据，把"编译/测试脚本跑通"与"需求指标达成"画等号，导致无验收数据的运行被虚假判为通过，且无法区分技术通过但业务失败（或技术未跑但人工已签发）的情形。
2. **派生运行策略环境逃逸（P1-2）**：`herdr/replay_engine.py` 在重构或回放一个历史 Run 时，若源 Task/Workflow 没有显式冻结策略，直接降级读取当前运行进程的全局配置或 CLI 默认策略，导致当前环境的全局策略（例如 `HERDR_STAGE_POLICIES`）向历史回放静默泄漏，破坏了“默认使用 frozen snapshot”的隔离性。
3. **血统谱系过早落盘（P2-1）**：在启动派生任务时，先写 `ReplaySpec`（记录 parent_run_id 与 child_run_id）再执行 `herdr-task launch`。当 launch 因调度、进程崩溃或参数校验失败时，或者由于并发竞态导致生成的 Task 实际并未继承 `replay_of` 时，数据库中已留存不可逆的孤儿谱系，造成 lineage 虚假。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 需求验收（Requirements）借用技术验证（Verification） | 语义分层混乱：Verification 是技术证据，Requirements 是业务/阶段裁决 | `requirements_satisfied` 仅由权威验收事实（`acceptance_verdict`、`stage_verdict`）产生，无事实一律返回 `null`（保持三态），严禁反向猜测与跨层代行 |
| 历史回放时全局配置静默污染 | 派生运行必须自闭环，当前宿主环境的动态配置不得污染历史重放 | 建立严格的 5 级策略继承链（ReplaySpec 策略 → 源快照 → Task 冻结 → Workflow 冻结 → 私有 definition），无来源时 policy 显式为 `null` 并标明 `policy_source: unavailable` |
| 先写谱系再启动派生，失败留孤儿 | 副作用顺序颠倒：尚未确立真实身份即持久化关系 | **必须在 launch 成功且双向核验身份**（`run_id == replay_run_id` 且 `replay_of == source_run_id`）后才写入 `ReplaySpec`；launch 失败或身份不一致必须执行包含 Task/Workflow/事件/快照文件的完整级联原子补偿 |

### 操作规范

```bash
# 1) 验收推导无猜想、双向解耦验证（herdr/eval_engine.py）
# 验证：仅读取 acceptance_verdict 与 stage_verdict，无 LLM judge，无伪 score
python3 -m pytest tests/test_eval_engine.py -k "test_eval_requirements_satisfied"

# 2) 回放策略来源 5 级继承链与防泄漏验证（herdr/replay_engine.py）
# 注入全局环境策略，确认源无 policy 时输出 policy=null / policy_source=unavailable
HERDR_STAGE_POLICIES='{"mode":"GLOBAL-LEAK"}' python3 -m pytest tests/test_replay_engine.py -k "policy"

# 3) 谱系写入时机与原子补偿验证（P2-1）
# launch 失败或身份校验失败时，断言 ReplaySpec 行数为 0，且 Task/Workflow/Event 彻底级联清理
python3 -m pytest tests/test_replay_engine.py -k "compensation"
```

### 验证命令 / 证据

```bash
# 1) PR86 候选提交与改动清单（7 文件 +348 -53）
git show --stat cc5e9a0
# bin/herdr-task                   |  10 ++-
# docs/architecture/eval-replay.md |   8 +-
# herdr/eval_engine.py             |  12 ++-
# herdr/eval_store.py              |  13 ++++
# herdr/replay_engine.py           | 157 ++++++++++++++++++++++++++++++-------
# tests/test_eval_engine.py        |  38 +++++++--
# tests/test_replay_engine.py      | 163 ++++++++++++++++++++++++++++++++++++---

# 2) 全量测试回归与 lint 基线
pytest -q  # 1191 passed, 44 subtests
ruff check bin/herdr-task herdr/eval_engine.py herdr/eval_store.py herdr/replay_engine.py  # 0 errors

# 3) 独立评审结论（review-auto-r3）
# MERGE_READY 十条条件全通过，阻断缺陷 0 项，7 项非阻塞建议汇总追踪
```

- 门禁证据（shared `notes.jsonl`）：
  - `impl-fix6`：`n-1790163114271-36f8`（P1-1 解耦、P1-2 策略继承链、P2-1 身份校验后写 spec，全量 1191/1191 绿）
  - `test-auto-r8`：`n-1790163892258-aee0`（clone detached@cc5e9a0 验证，1191/1191 pass，触改文件零新增 lint，Compare 四字段）
  - `review-auto-r3`：`n-1790165060831-9aed`（MERGE_READY 明确通过，阻断缺陷 0）

### 相关文档 / 关联证据

- `docs/architecture/eval-replay.md`（Eval Engine、Replay Engine 与 Compare 架构规范）
- `herdr/eval_engine.py`、`herdr/replay_engine.py`、`herdr/eval_store.py`
- `wiki/log.md`（fix6 轮收尾条目与二次收尾关系）
- PR: https://github.com/allinai0506/HAFlow/pull/86

## 99. 单页应用的共享渲染容器必须有单一所有权者；部署资产必须与运行副本同源

### 问题背景

PR #114（Flow Workbench v1，`c2faa61`）把 Console 的 Workflow 主区从 Stage Stepper 升级为 DAG 画布，默认视图改为 Flow，其中把共享的 `#tasks` 容器置为 `display:none`。但 `#tasks` 并非 Task List 独占——运维驾驶舱、我的仪表板、历史/未注册空间面板三处既有代码都把自己的内容写进同一个容器。

用户实测反馈：「我的仪表板为什么打开也是这个页面」。内容确实渲染了，但被 Flow 默认视图的 `display:none` 隐藏，屏幕上只剩画布。独立审查进一步查出同源的两个既存缺陷：`showOpsCenter` 未清 `dashMode` 也未停 10s 定时器，导致仪表板定时器周期性覆盖运维驾驶舱内容、且「← 返回工厂」退不出来；aux 模式下 `.task-filters` 仍可点击，`renderTasks` 覆写掉运维/仪表板内容。

同一 PR 还暴露一个部署缺陷：`scripts/install-herdr-console.sh` 只 `install` 三个文件，不复制 `console/static/`，导致新引入的离线依赖在部署环境必然丢失，Flow Canvas 会稳定报「本地 X6 资源缺失」。开发态（直接在仓库跑）完全正常，只有走 LaunchAgent 部署才暴露。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 新视图把共享容器 `display:none`，其他既有视图写进同一容器 | 共享渲染容器有多个写入方时，**显示归属必须由单一入口裁决**，不能让每个视图各自决定 visibility | 抽出唯一裁决函数（如 `setWorkspaceMode(flow/list/aux)`），成为该容器及其兄弟节点 display 的**唯一**写入者；新增/修改任何写该容器的路径必须显式 claim |
| 症状是「页面没切换」，不是报错 | 静默的可见性缺陷比崩溃更危险：没有异常、没有日志，只有用户肉眼发现 | 契约测试必须断言「每个写共享容器的函数都调用了 claim」，而非只断言某一个函数正确；正向测试通过不代表其他写入方也被覆盖 |
| 模式互斥标志不对称（`showDashboard` 清 `opsMode`，`showOpsCenter` 不清 `dashMode`） | 多个布尔模式标志并存的 UI 天然会漂移；定时器与标志不同步 = 周期性互相覆盖 | 模式切换必须成对清标志 + 停定时器，并在切换函数里集中声明；测试断言切换函数体内的对称赋值 |
| aux 模式仍可点击属于其他模式的控件 | 控件可见性必须跟随容器模式 | 模式辅助函数统一处理所有模式相关控件（切换条、筛选器、摘要）的显隐，不留可点的旁路 |
| 部署脚本逐个 `install` 文件，新增目录型资产未被复制 | 部署资产清单与运行副本的同源性必须在脚本里显式维护 | 目录型资产（`static/` 等）用 `rsync -a --delete` 同步；新增运行时资源目录时必须同步更新部署脚本，并加「部署后资源可达」检查 |
| 开发态通过 ≠ 部署态通过 | 验证环境必须与真实运行入口一致 | Console 类改动必须在 LaunchAgent 部署形态下验证资源可达（HTTP 状态码），不只在仓库内跑 |

### 操作规范（已固化到 `wiki/flow-workbench.md` §6、`tests/test_console_flow_workbench.py`）

1. **容器归属单一入口**：新增或修改任何写共享渲染容器的代码路径，必须显式调用 `setWorkspaceMode(...)`；禁止在其他位置直接写该容器的 `style.display`。
2. **模式切换成对清理**：`showOpsCenter` / `showDashboard` 互斥进入时，必须同时清对方标志并停对方定时器。
3. **容器归属契约测试**：为「写共享容器的函数集合」逐个断言 claim 调用（`test_every_aux_container_writer_claims_the_workspace`），新增写入方时测试必须同步更新，否则会被门禁拦下。
4. **部署资产同源**：新增运行时资源目录时，同步更新 `scripts/install-herdr-console.sh` 的 `rsync` 规则，并验证部署后 `curl` 资源返回 200。
5. **空 catch 不得掩盖死代码**：若某个调用被 `try/catch` 吞掉，必须确认该 API 在 vendored 依赖里真实存在（X6 3.x 无 `cleanSelection`，selection 是插件）。用 `grep` 在 vendor bundle 里核对 API 存在性，而不是假设。

### 验证命令 / 守护测试

```bash
# 1) 容器归属 + 画布生命周期 + 依赖缺失 fail-soft + 无 CDN
pytest -q tests/test_console_flow_workbench.py
# 期望：15 passed

# 2) 反向验证（把 D1/D2/D3 三处修复回退，契约测试必须失败）
pytest -q tests/test_console_flow_workbench.py
# 期望：4 failed（test_every_aux_container_writer_claims_the_workspace /
#        test_select_space_drops_stale_flow_graph /
#        test_ops_center_clears_dashboard_mode_and_timer /
#        test_aux_mode_hides_task_filters）

# 3) 部署态资源可达（真实 LaunchAgent 形态）
./scripts/install-herdr-console.sh
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/static/vendor/x6-3.1.8.min.js
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/static/vendor/dagre-3.1.1.min.js
# 期望：200 / 200

# 4) 禁止第三方运行时 CDN
grep -riE "unpkg|jsdelivr|cdnjs" console/ herdr/ || echo "no CDN"
# 期望：no CDN
```

### 相关文档 / 关联证据

- `wiki/flow-workbench.md` — Flow Workbench 分层职责、真值来源表、状态聚合优先级、容器归属契约（§6）
- `herdr/workflow_graph.py` — `workflow_graph_projection` / `aggregate_node_status` / `pick_default_node`
- `console/herdr_factory_console.py` — `setWorkspaceMode` / `selectSpace` / `showOpsCenter` / `send_static`
- `scripts/install-herdr-console.sh` — `static/` 的 `rsync -a --delete` 同步
- `tests/test_console_flow_workbench.py` — 容器归属契约与反向验证测试
- Git Commit `c2faa61` / Merge Commit `c9bb16e`
- PR: https://github.com/allinai0506/HAFlow/pull/114

---

## 100. 活性判据的输入必须匹配它的物理载体：折行的终端屏幕不是逻辑文档

### 现象

Workflow `wf-project-0929-01` 的 `plan` 节点卡在非终态 9.5 小时。现场证据齐备：

- `plan-arch` = `cleaned`（同一节点、同一批次）；
- `plan-adversarial` 两份核心产出全部落盘 —— 技术方案与任务拆分（64KB）、
  方案对抗审查与可行性风险评估（158KB），`notes.jsonl` 也有对应 `kind=gate` 台账；
- `herdr agent get w13:pB` = `idle`；
- `completion_observations` 里 `observed_version=3` 与 `tasks.version=3` 一致，
  `agent_status=idle`、`epoch_changed=0`、`vanished=0`。

唯独 `marker_present=0`、`consecutive_samples=0`，即
`compare_and_set_completion_transition` 永远以 `completion_marker_absent`
拒绝，`working → agent_done` 永不发生，节点永不推进。

### 根因（实测，非推断）

`herdr pane read w13:pB --source visible` 原文：

```
     HERDR_TASK_DONE:plan-adversarial-unified-task-
     workbench-v1
```

标记**确实在屏幕上**，但被终端硬折成两行。而 Sentinel 与 Controller 各自用裸子串
匹配（`services/herdr-sentinel.py` 与 `services/herdr-controller.py` 的
`f"HERDR_TASK_DONE:{task_id}" in screen`）去读**已折行的屏幕**——标记不再是一段
连续子串，于是永远匹配不到。

标记长度 = `len("HERDR_TASK_DONE:") + len(task_id)` = 16 + 42 = **58**；
同节点兄弟 `plan-arch-*` = 16 + 35 = **51**。opencode TUI 消息列宽度落在
51 与 58 之间：`plan-arch` 单行放下所以顺利推进，`plan-adversarial` 超宽被折
所以永久卡死。**该缺陷与产物质量无关，只取决于 task_id 长度与 Pane 宽度之差**，
换个更长的 task_id 就会在任意 workflow 上复现。

`herdr/completion.py` 是 FR-1 的纯决策层，承载了 elapsed / 采样间隔 / epoch
等全部时间与轮次判据，唯独**标记探测**这一环漏在 I/O 层用裸字符串实现，
纯层与装配层之间没有共同契约。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 对折行的终端屏幕做连续子串匹配 | 活性判据的输入载体与判据的假设不匹配时，判据恒为假，且**无异常、无日志、无告警** | 标记探测下沉为纯函数 `herdr.completion.marker_present()`；只消解**缩进续行**（TUI 折行必带左边距），硬换行保留换行符 |
| 只有裸子串匹配能匹配到「别的 task 的标记」 | 证据必须与主体绑定，否则跨 Task 误完成 | 命中后做**标识符边界校验**：`...-v1` 不得满足 `...-v1b` |
| 「空白行」与「缩进续行」都被当成可拼接换行 | 拼接规则太宽会把两个独立块拼成假标记 | 软折行正则收紧为 `\r?\n[ \t]+(?=\S)`：纯空白行仍视为硬块边界 |
| 纯层测试全绿仍可能被绕过（有人把判断重新内联回 I/O 层） | 函数测试不覆盖「调用点有没有用它」 | 加**源码级契约测试**：`f"HERDR_TASK_DONE:{{task_id}}"` 出现在任一守护进程即失败 |
| 契约测试断言源码文本时，可能把缺陷实现本身写成契约 | 断言会随重构失效，甚至反向锁死错误实现 | 重定向到新接缝并**加强**（从「存在裸子串」升级为「必须走共享接缝 + 状态语义不变」），不删测试 |
| 三个标记前缀（done / blocker / orch）各自实现 | 同一屏幕、同一折行问题，同类缺陷三份 | 三个前缀共用同一接缝，仅参数化 prefix |

### 操作规范（已固化到 `wiki/task-lifecycle.md` §1.3）

1. **单一接缝**：任何 Pane 标记探测必须调用 `herdr.completion.marker_present()`，
   禁止在 `services/` 内联 `f"{PREFIX}:{task_id}" in screen`。
2. **保守消解**：只消解缩进续行；空行、纯空白行、无缩进行一律保留换行。
3. **主体绑定**：命中后必须过标识符边界校验，跨 Task 证据不得通用。
4. **源码级门禁**：`test_daemon_has_no_raw_marker_substring_check` 覆盖两个守护进程
   × 三个前缀，共 6 条；回归必须失败才算守住了。
5. **验收不接受「产物已落盘」代替生命周期信号**：本例产物与台账全齐仍卡死，
   说明产物齐备度不能替代 `working → agent_done` 的因果链验证。

### 验证命令 / 守护测试

```bash
# 1) 纯层行为 + 源码级契约
pytest -q tests/test_completion_marker_wrapping.py
# 期望：27 passed

# 2) 反向验证（把匹配退回裸子串，契约必须失败）
#    在 services/herdr-controller.py 临时插入：
#      return f"HERDR_TASK_DONE:{task_id}" in screen, screen
pytest -q tests/test_completion_marker_wrapping.py
# 期望：test_daemon_has_no_raw_marker_substring_check 失败

# 3) 真实活 Pane 判定（不是夹具）
python3 -c "
import subprocess,sys; sys.path.insert(0,'.')
from herdr.completion import marker_present
tid='plan-adversarial-unified-task-workbench-v1'
r=subprocess.run(['herdr','pane','read','w13:pB','--source','visible'],text=True,capture_output=True)
s=r.stdout+r.stderr
print('legacy:', f'HERDR_TASK_DONE:{tid}' in s, '| fixed:', marker_present(s,tid))"
# 期望：legacy: False | fixed: True

# 4) 全量回归
pytest -q
# 期望：2406 passed, 50 subtests passed
```

### 相关文档 / 关联证据

- `wiki/task-lifecycle.md` §1.3 — 完成标记折行容错契约
- `herdr/completion.py:marker_present, marker_literal` — 纯探测接缝
- `services/herdr-sentinel.py` / `services/herdr-controller.py` — 4 处调用点
- `herdr/state_db.py:compare_and_set_completion_transition` — `completion_marker_absent` 拒绝分支
- `tests/test_completion_marker_wrapping.py` — 行为 + 源码级契约
- `tests/test_inner_loop_protocol.py` — 契约断言重定向到新接缝

---

## 101. 已知不可能赢的 CAS 必须前置跳过，而不是每轮 sweep 重试到偶然对上

### 现象

`wf-project-0929-01` / `impl-t6-mock-retire`（qodercli）在 08:29:14 启动，4 秒后
Sentinel 上报 `blocked_marker_observed`（`observed_version=3`）。08:29:46 有一次
`herdr-task set-status`，任务版本被抬到 5。此后到 08:54:04 落 `blocked` 为止，
Controller 每轮 sweep 都用**同一份 `observed_version=3` 的旧样本**去 CAS：

```
select count(*) from events
 where task_id='impl-t6-mock-retire'
   and event_type='blocked_observation_cas_rejected';
-- 238
```

**238 条完全相同的拒绝事件，跨 25 分钟，任务零进展。** 直到 08:53:58 Sentinel 碰巧
再次看见标记、写入 `observed_version=5` 的新样本，CAS 才在 08:54:04 成功。也就是说
这次成功**纯属侥幸** —— 靠 Sentinel 何时重新看见屏幕上的标记。

### 根因

`process_blocked_observations` 读 `blocked_marker_observed` 事件列表（limit=1 desc），
把事件里的 `observed_version` 交给 `kernel.transition_task`，被拒就记一条事件，然后
`continue` —— 下一轮 sweep 原样重来。代码注释甚至已经写明
"An old event can never win after a human reopen or another Controller update"，
**但注释描述的是事实，代码却仍在尝试**。

两条独立缺陷：

1. **无前置检查**：明知样本已陈旧（version 对不上）仍发起 CAS。
2. **拒绝事件无去重**：每轮 sweep 往 events ledger 追加一行同样的事实。

对比：完成观测通路（`compare_and_set_completion_transition`）是**单事务内**校验 +
CAS + 消费观测，天然不会重试同一份样本，全库拒绝计数个位数。**只有 blocked 通路
把观测读在事务外**，才暴露出这个活性空洞。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 注释说"旧事件不可能赢"，代码却照发不误 | 注释不是护栏；**不可能赢的重试必须是结构上不发生** | 陈旧样本在发起 CAS **之前**用纯判据跳过，静默等 Sentinel 补新样本 |
| 观测读在事务外 + 每轮 sweep 重试 | 事务外读到的观测会随权威行漂移，重试只会放大漂移 | 事务外读观测的通路必须做 version/status 前置绑定；能进事务的（完成观测）就进事务 |
| 每轮 sweep 追加一条相同拒绝 | 事件 ledger 是事实流，不是重试日志 | 非预期拒绝按 `(task_id, observed_version)` 去重，一个样本一条事实 |
| 去重状态无生命周期 | 进程内 map 会随任务数无界增长 | 任务离开 active 集合时同步清键 |
| 陈旧只提示一次 | 同一事实每 2s 刷屏会淹掉真信号 | 陈旧闩也去重：只在"首次陈旧"和"版本变化"时打一行 |

### 操作规范（已固化到 `wiki/task-lifecycle.md` §1.4）

1. **前置判据纯函数化**：能否发起 CAS 由 `herdr.completion.observation_is_current()`
   判定，Controller 与 Sentinel 不得各写一份。
2. **fail-closed 不变**：判据只减少"明知会拒的尝试"，不放宽任何已有拒绝。
3. **不绑定即放行**：样本或权威行缺 version 时返回 True，交由权威 CAS 裁决 ——
   避免用缺字段误杀新鲜样本。
4. **非法 version fail-closed**：`"v5"` / `""` / 非数字一律判为陈旧。
5. **去重键含样本身份**：`(task_id, observed_version)`；新样本必须能再次触发记录。

### 验证命令 / 守护测试

```bash
# 1) 纯判据 + Controller 接线
pytest -q tests/test_blocked_observation_cas_storm.py
# 期望：15 passed

# 2) 反向验证（回退前置检查与去重，契约必须失败）
#    services/herdr-controller.py 内把
#      if not observation_is_current(...):
#    改为
#      if False and not observation_is_current(...):
#    并把
#      if _blocked_observation_rejected.get(task_id) != expected_version:
#    改为
#      if True:
pytest -q tests/test_blocked_observation_cas_storm.py
# 期望：3 failed（test_stale_sample_is_never_attempted /
#        test_unexpected_rejection_is_recorded_once_per_sample /
#        test_a_new_sample_retries_and_is_recorded_again）

# 3) 现场数据回放（真实 events，不造夹具）
sqlite3 ~/.herdr-controller/state.db "
  select count(*) from events
   where task_id='impl-t6-mock-retire'
     and event_type='blocked_observation_cas_rejected';"
# 修复前：238

# 4) 全量回归
pytest -q
# 期望：2426 passed, 50 subtests passed
```

### 相关文档 / 关联证据

- `wiki/task-lifecycle.md` §1.4 陈旧观测的 CAS 前置跳过
- `herdr/completion.py:observation_is_current` — 纯前置判据
- `services/herdr-controller.py:process_blocked_observations` — 跳过 + 去重
- `tests/test_blocked_observation_cas_storm.py` — 判据 + 接线 + 反向验证
- 现场：events 表 `impl-t6-mock-retire` 238 条 `blocked_observation_cas_rejected`

---


## 102. Worktree 的 .git 指针不具备 CoW 隔离（2026-09-30）

### 问题背景
`wf-project-0929-01` 三个任务复制 Gemini Worktree 后共享 `.git/worktrees/gemini`，T6 切分支改变了 T1 与 Barrier-0 的 HEAD，导致提交触发 Agent/branch mismatch，方案未进入源仓库。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| `create_clone` 仅检查 `.git` 存在，误把复制工作文件视为 Git 身份隔离；复制 `.git` 文件不会复制其目标 HEAD/index。 | 工作文件复制不等于 Git 身份隔离 | 修改 HEAD/index 前验证独立元数据 |

### 操作规范（已固化到源码与回归测试）
在后续 Git 副作用前，将 Worktree 指针替换为独立 clone 元数据。现场恢复必须保留工作文件、备份指针/index/HEAD，分别恢复任务分支；源暂存区污染单独核对，不以改 Agent 名称绕过门禁。

### 验证命令 / 守护测试
`TestCleanSandbox.test_worktree_source_clones_have_independent_git_state` 使用真实 Git Worktree、两个 Clone 和 staged WIP；断言源 HEAD/index/工作文件未变、两个 Clone 分支/index 不相互污染。修复前断言 `.git.is_dir()` 失败，修复后专项 22 passed。


## 103. runtime done 必须贯穿完成判定与持久化（2026-09-30）

### 问题背景
T1 已完成且全量评估 3961/3961，Herdr 返回 `done`，Sentinel 却持续记录 `EARLY marker present while busy`。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| Herdr 的 done 表示已完成但尚未被查看，HAFlow 纯完成策略与 SQLite observation/CAS 仍只接受 idle，运行时与持久化的语义不一致。 | 完成态语义必须贯穿策略与持久化 | done 仍须通过标记、稳定性与 CAS 门禁 |

### 操作规范（已固化到源码与回归测试）
在完成策略、observation ready、completion CAS 三处一致接受 idle/done。done 不能单独完成任务，仍要求新标记、稳定双采样、最短耗时、当前身份与版本。

### 验证命令 / 守护测试
`test_done_runtime_completion_persists_with_existing_gates` 修复前因 done 被分类 early 失败；修复后经真实 SQLite observation 与 CAS 到 agent_done，同时拒绝无标记、短耗时、过期 epoch。


## 104. 本地 Agent 锚点不是远端分支（2026-09-30）

### 问题背景
T1/T6 完成后已 committed，integrate 对 `origin/agent/gemini-init` 的 fetch 报找不到远端 ref，反复终化无法收敛。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| `base_branch` 可来自本地 Worktree 锚点，integrate 却统一按 origin 分支解析，忽略源现场本身是可用基线。 | 本地锚点不能假定存在于远端 | 按基线归属选择获取路径并沿用既有锁 |

### 操作规范（已固化到源码与回归测试）
仅对 `agent/*-init` 在既有 source/Clone 锁内读取源本地分支到 Clone 的 task-scoped base ref，rebase/关系验证统一使用该 ref；普通分支保持远端路径。

### 验证命令 / 守护测试
真实 Git+SQLite `test_local_agent_anchor_integrates_without_remote_anchor` 修复前重现 remote ref 缺失；修复后 source HEAD/anchor 不变，集成 ref 同时包含基线推进和任务成果。集成专项 51 passed，实际 T1/T6 从 committed 到 integrated。


### 第104节补充：冻结候选的本地续接必须显式绑定完整SHA（C12）
本地冻结分支未发布到origin时，launch和Worker的--onto原先统一要求远端，候选存在仍被拒绝。新增路径仅允许显式完整候选SHA与本地ref的原生commit ID逐字一致；源预检在运行资源创建前，Worker在独立Clone检出前后重新核对。无pin仍保留远端路径，移动分支/缩写/符号revision拒绝，活跃分支所有权不绕过。真实Worker CoW回归保留源WIP并只清理隔离Clone；不push、不启动真实Agent，后续门禁不放宽。
专项最终46 passed，撤销关键实现7 failed/2正常对照passed，恢复9靶向passed；初次fixture的clone origin误指向源而非源的远端，已用真实Worker create_clone替代并保留旧日志。首次自审发现符号revision可被解析成当前SHA，3项失败测试锁定该路径，随后改为完整原生ID比较。最终专项127 passed；全量2682 passed/145 subtests、0 failed/0 skipped（420.72s）。中间全量1 failed/2681 passed：原selective-replan夹具给所有Git返回ok，身份校验在预检拒绝，未进入其原目标Worker缺基线回收。改为真实临时Git候选与本地bare origin，保留全部拒绝/回收/不登记/审计断言；失败日志保留。此前2679通过记录不替代最终源码验收。仅自审、未部署，无真实Agent/model调用。

## 105. 绿色测试名含 FAIL 不等于失败（2026-09-30）

### 问题背景
Vitest 输出绿色 `✓ ...展示 FAIL 状态...`，3961 个测试已通过，评估器仍把该行加入 failing_tests，得分被压到95，内循环耗尽。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| Vitest/Jest 失败提取仅匹配行内 FAIL/✕ 子串，没有先识别行首通过标记，测试名称被误当状态。 | 测试名称不能充当执行状态 | 先识别通过标记再提取真实失败 |

### 操作规范（已固化到源码与回归测试）
复用 `_is_failing_test_line`，先排除以✓/√开头的通过行，再保留原真实失败识别；不改业务测试名称或降低评分门槛。

### 验证命令 / 守护测试
`test_loop_evaluator.py` 新回归覆盖Vitest/Jest、两种通过标记、FAIL/✕标题、混合真失败和Jest失败suite。修复前10个subtests失败；修复后绿标题仍100分/converged，真实失败仍留在failing_tests。

```bash
python3.13 -m pytest -q tests/test_herdr_worker.py tests/test_impl_fix1_regression.py tests/test_legacy_adopt_converge.py tests/test_loop_evaluator.py
```

关联证据：[工作流恢复记录](../walkthroughs/20260930-wf-project-0929-recovery.md)、`wiki/task-lifecycle.md`。

---

## 106. 已派发任务全部结束不代表实现计划全部交付

### 问题背景
`wf-project-0929-01` 在 T3/T4a/T4b/T7 尚未派发、T5 尚未提交时被判为 implementation 完成；test/review 持续 FREEZE DEFERRED。根因不是 verifier 少一个字段，而是交付上游未闭合。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| completed 被当作 Git 成果可用 | Agent 结束不等于持久化集成完成 | Git 节点等 integrated 后推进 |
| 只统计已派发任务 | 零活跃任务不能证明整个计划完成 | 已批准计划以显式 required_task_ids 核对，缺项不冻结 |
| UI 投影丢掉清单 | 写入约束须贯穿所有读取方 | Controller/node-status/ops 复用纯判据并保留配置字段 |
| 独立 clone 丢 source local heads | Git 身份独立与分支身份保留须同时成立 | 本地 refs 导入只写临时独立 metadata，不修改 source |

### 操作规范（已固化到源码与回归）
1. 不补造 Candidate/verifier 身份，不用 force-pass 把未完成实现变成通过。
2. Git 成果经正常 commit/integrate；源锚点仅在核对后推进，留恢复分支；基线变化后重跑测试。
3. 必需任务只由明确计划登记，不从自由文本猜 ID。替代只沿真实 superseded_by；清单为空 Task 时不能被 reuse 绕过。
4. 测试使用项目固定运行时。本次默认 Node 下 AbortSignal 类型不匹配，Node 22.16.0 全量通过，未削弱 SSO 断言。

### 验证命令 / 关联证据

```bash
python3.13 -m pytest -q tests/test_scheduler_dispatch_e2e.py tests/test_herdr_task_ops_center.py tests/test_herdr_worker.py tests/test_dispatch_candidate.py tests/test_selective_replan_controller.py
```

修前复现本地分支丢失、未集成提前完成、清单缺项、CLI投影丢字段；Controller→真实临时 Git→SQLite 冻结链证明缺计划项时不写 candidate_frozen，交付齐全后才写一条事实。详见 [恢复记录](../walkthroughs/20260930-candidate-recovery.md)。

---

候选恢复追加验证：完成判据必须通过真实 SQLite Workflow 记录→明确配置文件→normalize→Controller/CLI 验证，直接 mock workflow_config_for 会掩盖字段丢失。明确记录文件缺失时不得借用全局 legacy 的其他工作流配置。窄 Pane 造成 TUI idle 默认推断时须验证实际尺寸与身份；任务评估命令须对齐代码语言，不能用前端测试替代 Java Provider 验收。

### 第 106 节补充：门禁挡住提前完成之后，还需要阶段内部的交接恢复

#### 问题背景
`wf-project-0929-01` 的 T4a 于 2026-09-30 13:22:36 到达 cleaned，T3/T4a 的成果存在集成引用，但基线仍是 `d78809a32`，T4b/T7 未派发。阶段清单门禁能拒绝冻结候选，却不能替协调者继续同一节点内的计划。

#### 经验教训
发送阶段通知不是执行回执；任务结束不是指定版本采用成果。仅监控活跃 Task，会遗漏“工作流 running、零活跃 Task、计划缺项”的空档。将未完成义务绑定事实指纹，不能以消息发送成功清除。

#### 操作规范
复用 StateStore/显式任务清单/Git 证据，Controller 持久化有界恢复 episode；队列发送前重读事实，不直接合流或绕过候选与串行门禁。跨进程领取须在文件锁事务内完成。首次账本不存在应初始化空 episodes，损坏账本仍拒绝事务。

#### 验证命令与证据
`python3.13 -m pytest -q tests/test_workflow_continuation.py`：真实临时 Git 合流前后采用证据、临时 SQLite→配置→Controller→账本→Projection、两个独立进程竞争、重启读取、暂停/阻塞、旧队列和两次预算升级。外部 Herdr prompt 用受控传输替代，未启动真实 Agent 或改生产工作流。

---


### 第 106 节补充：任务尚未创建时，派发本身也需要恢复契约

#### 问题背景
首节点总指挥调用返回 0 却没有创建 Task，Controller 写 notified 并输出 STAGE ADVANCED。零 Task 绕过 continuation 和阶段悬挂检查。独立审查又复现发送前丢失内存队列、旧配置回落无跟踪发送、旧代次 Task 阻止新义务、终态历史挤掉当前义务。

同类缺口在有依赖的 test/review 再次出现：首节点 SQLite 闭环没有覆盖下游，旧 notified 与空任务组合仍能静默阻断。

#### 经验教训
外部命令返回与实际任务登记是两种事实。防重复闩必须配合有期限的持久责任；给 notified 加 TTL 然后盲目重发会把停滞变成重复副作用。查询必须在 LIMIT 前排除终态历史，代次过滤不能在下一层被全量历史判断撤销。

人工说明文字不是授权事实：hold 会覆盖 reason，重试保护必须核验当前取消任务及旧未结案交付。已被 reuse 满足的节点不能留下永远不会被调度的 pending 派发责任。

#### 操作规范
在同库登记派发义务，入队预占租约、发送前写 started、实际 CLI intent 与 Task 绑定 operation/Run/配置代次；未知交付只核验，到期转明确人工决定。JSON queued 不是队列恢复权威。等待期限不能被轮询或日志延长，hold 必须尊重原到期时间。 修复损坏 Task 载荷时，四个登记身份字段必须一起核对原持久 intent；只检查 operation ID 会把缺失 Run/execution/intent 误认为权威空值。缺失字段可以从原证据补齐，已有非空冲突必须拒绝，不能用新请求自证旧身份。

下游直接与协调器派发都绑定候选 episode、上游 Run 与真实 intent。已计划多角色时核对全部 Task；只有完整 resources_absent 否定回执才允许安全重试。旧测试替身只 return 0 会缺失登记证据，应保留真实 intent、Task 和后继谱系，不能放宽验收断言。

#### 验证命令与证据
`pytest -q tests/test_node_dispatch_contract.py tests/test_direct_stage_dispatch.py`。新增测试保留真实 Controller/CLI/SQLite，仅替换外部 Pane/Worker/Agent 传输；受控时钟、独立连接竞争和丢队列/旧配置/千条终态历史均有行为断言。初始复现 2 failed；本地验证与线上 Agent/部署验收分开报告。

下游回归：`pytest -q tests/test_downstream_dispatch_contract.py tests/test_reverification_controller.py`。首次六个下游契约用例 RED；独立审查复现 hold→retry 取消绕过及 reuse 空责任后，新增事实校验与相应行为回归。真实 CLI 下游测试保留临时 Git/SQLite/候选校验，仅替换外部 Worker/Pane/Agent；未操作生产工作流。

---

## 107. 语法检查通过 ≠ 功能可用：控制台"全绿但按钮打不开"

### 问题背景
PR #120（`feat/console-controller-decision-buttons`）为控制台补齐 Controller 动作按钮与
"待你裁决"提醒。首轮实现把动作卡渲染器从 `openControllerCockpitModal()` 的内联模板
提升为模块级函数 `controllerActionCard()`，却漏掉它仍引用外层函数的 `const catMeta`。
浏览器打开 Controller 弹窗即抛 `ReferenceError: catMeta is not defined`——整个交付面
（状态卡 / 卡点按钮 / 交付链路 / 裁决面板）一个都渲染不出来，功能与改动前无异。
同轮第二处同类死链：`openDecisionPanel()` 用 `JSON.stringify` 生成选项芯片的内联
`onclick`，双引号落在双引号 HTML 属性里被解析器截断，点击后是 `SyntaxError`。

**两个缺陷都是在全量测试 2494 项全绿的情况下进入工作树的。** 根因是既有前端测试的
验证方式只能证明"脚本可解析"，证明不了"脚本能跑"。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| `node --check` 只做语法分析 | 能发现 `SyntaxError`，**发现不了 `ReferenceError`**——作用域是运行期解析的，静态检查无从判断自由变量是否在可见作用域内 | 任何"内联逻辑提取为模块级函数"的改动必须补运行时执行测试，不能只靠 `node --check` |
| 测试断言退化为 `assertIn(字面量, 源码)` | 字符串 grep 与功能可用性无因果关系；缺陷 B1 的 9 条此类断言照样全绿 | 前端契约测试禁止只做字面量 grep，必须真正**调用**被测函数并断言其输出 |
| `JSON.stringify` 产出双引号 | 内联 `onclick="..."` 处于双引号属性内，双引号会在第一个 `"` 处截断属性，JS 侧得到残缺语句 | 生成内联事件处理器的值必须用单引号 JS 字面量，并对 `'`、`\`、`"`、`&`、换行做转义；封装统一 `jsArg()` |
| 提取函数时遗漏闭包依赖 | 内联模板的 `const` 提升到模块级函数后即失去作用域，是重构高频静默破坏点 | 提取后立即搜依赖符号，确认每处都在新作用域内可见 |
| 组件为空时整体消失 | 旧代码用 `if (blockers.length \|\| acts.length)` 包裹整个卡片渲染区，"无报错卡点但仍待交付链路"时整个区域不渲染 | 不同语义的动作（解卡 vs 推进链路）必须**独立**判定渲染条件，不共用一个布尔门 |

### 操作规范（已固化到源码与回归）
1. **运行时渲染测试为前端第一道门禁**：真实抽取控制台 `<script>`，在 Node + DOM stub
   下**实际调用** `openControllerCockpitModal()` / `renderDashboard()` /
   `openDecisionPanel()`，断言渲染产物含预期区块。这道门能同时抓住作用域错误与
   渲染条件错误；
2. **内联处理器逐条编译**：把渲染结果中所有 `onclick="..."` 提取出来逐条喂给
   `node --check`，杜绝属性截断类死链；
3. **引号转义走统一入口**：内联事件的值一律经 `jsArg()` 生成，模板内禁止裸用
   `JSON.stringify`；`jsArg()` 正确性由"HTML 属性解析 → JS 求值"往返测试守护；
4. **语义分区渲染条件相互独立**：交付链路 / 卡点解卡 / 待裁决三节各自判定，任一为空
   不得导致其余消失。

### 验证命令 / 关联证据

```bash
# 运行时渲染门禁：真实执行控制台 JS，捕获 ReferenceError / 属性截断
python3.13 -m pytest -q tests/test_console_cockpit_runtime.py
# 期望：8 passed

# 语法层门禁（保留，但明确它只覆盖可解析性）
python3.13 -m pytest -q tests/test_console_frontend_syntax.py
```

**反向验证**：删除模块级 `catMeta` 定义后，
`test_cockpit_opens_and_renders_pipeline_section_without_blockers` 实际捕获到
`THREW ReferenceError: catMeta is not defined`——证明该测试能红，不是恒真断言。
篡改 `GIT_PIPELINE_FORWARD` 的 `committed` 行后 3 条测试失败。

### 相关文档 / 关联证据
- PR #120 — 核心改动（`herdr/controller_actions.py`、`herdr/human_decisions.py`、
  `console/herdr_factory_console.py`、`bin/herdr-task note-add --field`）
- `tests/test_console_cockpit_runtime.py` — 本次新增的运行时门禁
- `tests/test_console_decision_ui.py` — 源码级契约断言（仅作补充，不作唯一依据）
- `docs/walkthroughs/20260930-console-controller-decision-buttons.md` — 交付记录

## 108. `launchctl kickstart` 不重载 plist：改了配置等于没改（2026-09-30）

### 问题背景

给控制台按钮改中文文案（提交 `348f14c` / `4d8e177`）后，按 RULES 走
`launchctl kickstart -k gui/$UID/com.user.herdr-factory-console` 热重载。命令成功、
进程 PID 也换了，但 `curl http://127.0.0.1:8765/api/workflow/controller-actions`
返回的按钮标题**一字未变**——"真实 re-drive""插话指导""紧急制动"全在。

逐层排查后确认：console 与 controller 的 launchd 配置写死了
`HERDR_ROOT=~/.herdr-controller/releases/<40位commit>`，那是一份 `git archive`
快照，内容等于当时的 HEAD。工作区改动既不在 console 脚本里，也不在它
`sys.path.insert` 后 import 的 `herdr/` 包里。四个服务实际加载的代码并不一致：

| 服务 | ProgramArguments | 加载的代码 |
|------|------------------|-----------|
| factory-console | `releases/<sha>/console/herdr_factory_console.py` | 冻结快照 |
| controller | `releases/<sha>/services/herdr-controller.py` | 冻结快照 |
| sentinel | `~/HAFlow/services/herdr-sentinel.py` | 工作区 |
| notifier | `~/HAFlow/services/herdr-notifier.py` | 工作区 |

而 `launchctl kickstart -k` **只重启进程，不重新读取 plist**：launchd 用的是已加载的
job 配置快照。实测把 plist 改成新快照路径后 `kickstart`，起来的进程仍执行旧路径
（`release b33fe9f…`）；必须 `launchctl bootout` 后再 `launchctl bootstrap` 才会
重新解析 plist。

**这个坑在同一个 session 里踩了两次**——第一次没察觉，第二次改完 plist 又先
`bootstrap` 才改 plist，等于没改。

附带：`scripts/install-herdr-console.sh` 会把 console 脚本部署到
`~/.herdr-console/`，但它**不改 plist**。plist 指向 release 之后，那份部署副本
永远不会被执行，却仍然每次被 install 刷新——一个静默的误导源。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| `kickstart` 后 PID 变了就认为部署成功 | PID 变化只证明进程重启，不证明**配置或代码**是新的 | 部署后必须核对"运行中进程的实际命令行 / 加载路径"，不能只看 PID |
| 改了 plist 但只用 `kickstart` | launchd 不重读已加载的 job 配置 | 改 plist 一律 `bootout` + `bootstrap`；`kickstart` 仅适用于**未改配置**时的纯重启 |
| 提交到 main 就等于生效 | release 快照按 commit 冻结，工作区改动永远不进快照 | 改完代码必须核对"运行中服务加载的是哪份代码"，与 `git rev-parse HEAD` 对齐 |
| 服务间部署语义不一致 | console/controller 走冻结快照、sentinel/notifier 直跑工作区 | 部署拓扑是**必须显式记录的事实**，不能靠推测；排查前先列"每个服务实际跑哪份代码" |
| install 脚本与实际部署模型脱节 | `install-herdr-console.sh` 部署的文件与 plist 指向的路径不是同一处 | 部署脚本与 plist 指向必须同源，否则脚本是纯误导 |

### 操作规范（已固化）

1. **改 plist 后一律重启 job**：
   ```bash
   launchctl bootout gui/$(id -u)/<label>
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
   ```
2. **部署后三查**：`lsof -nP -iTCP:<port> -sTCP:LISTEN -t` 拿 PID →
   `ps -o command= -p <pid>` 看实际加载路径 → 与 `git rev-parse HEAD` 比对；
3. **重建 release 快照**：`git archive --format=tar <sha> | tar -x -C ~/.herdr-controller/releases/<sha>`，
   旧快照保留作回滚（本次保留 `b33fe9f`）；
4. **`lsof -p PID -iTCP` 是 OR 不是 AND**：不加 `-a` 会把该进程所有 fd
   （含 dylib、`/dev/null`）**加上**系统上所有网络连接一并列出。实测一个 console
   进程因此被误报成"监听 98 个端口"（实含 3306/6379/3000 等他人服务），
   真实 LISTEN 只有 1 个。正确写法 `lsof -a -p PID -iTCP -sTCP:LISTEN`。

### 验证命令 / 关联证据

```bash
# 部署后核对：运行中进程加载的是哪个快照
ps -o command= -p "$(lsof -nP -iTCP:8765 -sTCP:LISTEN -t | head -1)" | grep -oE '[a-f0-9]{40}'
git rev-parse HEAD
# 期望两者一致

# 真实 HTTP 验收新文案已生效
curl -s http://127.0.0.1:8765/ | grep -c "继续推进"          # > 0
curl -s http://127.0.0.1:8765/ | grep -c "推进交付链路"       # 0
```

**本次实测**：console PID 69225 / controller PID 69230 均加载
`4d8e1770ff5cbd7656209cb557e1ef5b847eb4ce`，与 `origin/main` 一致；页面新文案
命中、`推进交付链路`/`真实重驱`/`实时插话`/`紧急制动` 全部 0 次。

### 相关文档 / 关联证据
- 提交 `348f14c`（中文按钮）、`4d8e177`（补齐漏网术语）
- `tests/test_console_plain_chinese_ui.py` — 旧术语门禁
- `console/HerdrDashboard.command` — 双击入口仍用 `kickstart`（配置未变时可继续使用）

## 109. 补派换 ID 后 `superseded_by` 断链，工作流永久卡死（2026-09-30）

### 问题背景

`wf-project-0929-01` 的 implementation 节点卡在 `[STAGE ADVANCE WAIT]
coordinator=working`。`required_task_ids` 9 项里，`impl-t7-integration-gates`
处于 `superseded`（auto-recover 因 `dispatch_delivery_fuse` 作废），真正干完并
`cleaned` 的是补派出来的 `impl-t7-integration-gates-r2`。

`herdr/scheduler.py::node_is_complete` 本来就有 superseded 链式解析——顺着
`superseded_by` 一直走到落地的替代者。但 T7 的 `superseded_by` 是 `None`，链断在
第一跳，判定恒为 `False`。实测确认这就是唯一卡点：

```
现状（superseded_by 为空）:    node_is_complete = False
模拟回填 superseded_by → r2:   node_is_complete = True
```

**根因是两段式流程中间没人连线**：

1. `services/herdr-controller.py:1447` 的 auto-recover 调
   `herdr-task supersede <id> --reason "auto-recover: infrastructure failure"`，
   此刻**还不知道**将来替代者是谁，因此不传 `--by`；
2. 之后 `direct_dispatch.plan_stage_dispatch` 按谱系
   （`lineage_redispatch_candidates` + `next_replacement_id`）补派 `-r2`，
   spec 里带了 `redispatch_of`，但 `services/herdr-controller.py:4113` 构造
   launch argv 时**只把它拼进 prompt 文本，从未转成 `--supersedes`**。

`bin/herdr-task launch --supersedes` 机制本来是完整的（会调
`supersede_task(old, new_task_id=new)` 写 `superseded_by`），但补派路径没用它。

补派之所以不传 `--supersedes`，是因为传了也跑不通：`TRANSITIONS["superseded"]`
是空集（`superseded` 是终态），而旧任务**已经**是 `superseded`，`launch --supersedes`
会因 `superseded → superseded` 非法而 `exit 2`。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 两段式流程（先作废、后补派）中间无连线 | 谱系信息在 `task_id` 后缀里（`-r2`），**没有**落到 `superseded_by` 字段上；两个真相源不一致 | 凡"作废 + 补派"成对出现，必须回填 `superseded_by`，且由**回归测试**钉住 |
| 判据依赖可选字段 | `node_is_complete` 依赖 `superseded_by` 才能穿透 superseded，但该字段在补派路径下必然为空 | 判定所依赖的字段，其**每条写入路径**都必须被测试覆盖；新增写入路径时同步补测试 |
| 跨 run 补派被误判为身份串号 | 补派按设计就是新执行：controller 保留 `--execution-id` 并注明 "sibling launches keep distinct run_ids"，新旧 `run_id` 天然不同 | run 相等性只适用于"同一执行的重新作废"；补挂链路必须允许跨 run，但仍要校验同 workflow |
| 核心已支持幂等自转移，前置表不支持 | `herdr.transitions.validate_task_transition` 明确允许 `old == new`（注释：including idempotent self-transitions），但 `bin/herdr-task` 自己的 `TRANSITIONS` 表把它挡了 | 修状态相关缺陷前先读核心状态机，别在前置表上找唯一真相；两表语义差异本身是缺陷 |

### 操作规范（已固化到源码与回归）

1. **`supersede_task` 支持补挂**：旧任务已是 `superseded` 且 `superseded_by`
   为空时，只回填指针，**不动** `status` / `run_id` / `supersede_reason`；
   已有 `superseded_by` 时拒绝改指（防谱系被静默改写）；跨 workflow 一律拒绝；
   补挂允许跨 `run_id`（补派即新执行），首次作废仍要求同 run；
2. **补派链路传 `--supersedes`**：`services/herdr-controller.py` 构造 launch
   argv 时消费 `spec["redispatch_of"]`；
3. **不依赖自定义状态机参数**：补挂借道核心的幂等自转移能力，不新增
   `allow_noop_status` 之类的旁路开关；
4. **存量断链用同一命令修复**：`herdr-task supersede <old> --by <new>` 走的就是
   补挂分支，修复与预防共用一条代码路径。

### 验证命令 / 关联证据

```bash
# 7 条新回归：补挂写指针 / 保留终态与 run / 拒绝改指 / 拒绝跨 workflow /
#             允许跨 run / 无替代者时仍拒绝 / required_task_ids 穿透
python3.13 -m pytest -q tests/test_stage_advance_and_supersede.py
# 期望：25 passed, 9 subtests passed

# 真实状态机穿透验证
python3.13 -c "
import sys; sys.path.insert(0,'.')
from herdr.state_store import get_state_store
from herdr.scheduler import node_is_complete
s = get_state_store()
req = ['impl-barrier0-plan-rectify','impl-t1-contract-foundation',
       'impl-t2-archive-ocr-provider','impl-t3-recover-abnormal',
       'impl-t4a-aggregate-service','impl-t4b-task-http-contract',
       'impl-t5-frontend-workbench','impl-t6-mock-retire-r3',
       'impl-t7-integration-gates']
# 必须传 node 的**全量**任务：链式解析要按 superseded_by 找到 -r2，
# 只传 required_task_ids 清单会因找不到替代者而恒为 False
allt = [t for t in s.list_tasks('wf-project-0929-01')
        if (t.get('node') or t.get('stage')) == 'implementation']
print(node_is_complete(allt, req))   # 期望 True
"
```

**反向验证**：`TestNodeCompleteAfterRedispatchLink` 同时断言断链时为 `False`
（fail-closed）与回填后为 `True`，避免测试退化为恒真断言。

**本次实测**：补挂后 T7 保持 `status=superseded`、`run_id` 与
`supersede_reason` 均未变，`superseded_by=impl-t7-integration-gates-r2`，
`node_is_complete` 转为 `True`，controller 日志不再刷 `STAGE ADVANCE WAIT`。

### 相关文档 / 关联证据
- `bin/herdr-task::supersede_task` — 补挂分支
- `services/herdr-controller.py` — launch argv 补 `--supersedes`
- `herdr/scheduler.py::node_is_complete` — 链式解析（未改动，问题在数据）
- `herdr/direct_dispatch.py::lineage_redispatch_candidates` — 谱系补派去重
- `tests/test_stage_advance_and_supersede.py` — 7 条新回归

## 110. `--onto` 指向未推送的任务分支，test/review 派发必然失败（2026-09-30）

### 问题背景

§109 修好补派断链后，`wf-project-0929-01` 推进到 test/review，却立刻出现另一层卡点。
controller 日志反复刷：

```
[DIRECT DISPATCH ERROR] task=wf-project-0929-01-test-auto: Onto branch not found on origin: agent/opencode/feat-impl-t7-integration-gates-r2
```

`test-...` 与 `test-...-r5` 两条任务在 `pending` 阶段就带
`failure_reason=router_isolation_rejected` 死掉，从未真正执行。

### 根因：`--onto` 承载了两个语义，只有一种被校验

1. **语义 A（fix-loop 续接）**：`bin/herdr-task:2877-2900` 要求 `--onto` 必须存在于
   `refs/remotes/origin/`，注释写明"任务必须落在既有分支（如开放中的 PR 分支）上"。
2. **语义 B（测候选交付物）**：`herdr/direct_dispatch.py::candidate_branch_for_node`
   取依赖节点里 `updated_at` 最新的**任务分支**作为 onto。

controller 一律走语义 B，却撞上语义 A 的校验。而该工作流的 9 个
`integration_mode=git` 任务**全部已 `cleaned`** —— 交付物早已合入 base
`agent/gemini-init`（HEAD = `5d3d615e0`），**而任务分支从未推送到 origin**
（连 base 都没有）。于是 onto 指向一个 origin 上不存在的本地分支，
launch 必然 `exit 2`。

反证很直接：同一工作流里手工 `herdr-task launch`（不带 `--onto`）的
`test-...-r6` 成功落地，且 `base_branch=agent/gemini-init`、
`baseline_commit=5d3d615e07ab` 正是合入 base 的交付点 —— 证明"落在 base 上测"
才是正确形态。

另注：日志里的 `router_isolation_rejected` 与本条**无关**，那是真实的 router 失败
（`test-...` 是 agent 复用策略拒绝，`r5` 是 `claude` deep preflight 失败），
由 `bin/herdr-task::_record_router_failure_task` 在 `choose_agent` 抛错时写入。
排查时必须看 `failure_detail` 而非 `failure_reason` 标签。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 一个参数承载两种语义 | `--onto` 同时用于"续接外部 PR 分支"和"测本地候选交付物"，但只用一种校验 | 同一参数承载多语义时，校验必须覆盖全部语义；否则要么加参数区分，要么按语义分流 |
| 交付物已合入 base，却仍让 test 指向任务分支 | 任务分支是**过程产物**，base 才是**交付目标**；已 integrate 的任务，其 onto 不应是它的过程分支 | 候选分支计算必须区分"交付未完成"与"交付已完成"：前者用任务分支（fix-loop 需续接），后者用 base |
| 回退到本节点分支引入新错误 | 只加"跳过已交付依赖"后，onto 变成 `agent/pi/test-...-r6`（本节点正在跑的任务分支），同样未被推送、同样失败 | 修一半比不修更危险：回退路径必须一并纳入语义判断，并留测试覆盖 |
| 测试用假分支名导致假通过 | 夹具里写 `agent/pi/test-test-...-r6`（含 `...`）被 `sanitize_branch_name` 静默过滤，断言"返回 None"假通过 | 夹具必须用**真实形态**的标识；断言前先验证夹具数据真的能通过被测路径的前置过滤 |

### 操作规范（已固化到源码与回归）

1. **`candidate_branch_for_node` 新增 `delivered_in_base` 语义**：
   - `True` 且依赖为 `integration_mode=git` 且状态 ∈ {integrated, cleanup_ready, cleaned}
     → 跳过该任务分支（交付物已在 base）；
   - 依赖全部交付时**不**回退到本节点分支，返回 `None` 让任务落在 base；
   - 未交付依赖（working / blocked / failed 等）仍取其任务分支 —— fix-loop 依赖此行为；
   - 未传该参数时**保持原行为**，不静默改变既有调用方。
2. **controller 传 `delivered_in_base=True`**（`services/herdr-controller.py`），
   因为它算的是"要测的候选交付物"，前提就是交付已进入 base。
3. **判失败原因看 `failure_detail`**：`failure_reason` 是粗粒度标签，
   `router_isolation_rejected` 这类标签可能出现在与 router 无关的路径上。

### 验证命令 / 关联证据

```bash
# 5 条新回归：全交付→None / 未交付→仍用任务分支 / 不回退本节点 /
#             legacy 调用不变 / 覆盖三种 git 交付状态
python3.13 -m pytest -q tests/test_dispatch_candidate.py
# 期望：17 passed, 3 subtests passed

# 真实数据复算候选分支
python3.13 -c "
import sys; sys.path.insert(0,'.')
from herdr.state_store import get_state_store
from herdr import direct_dispatch as dd
s = get_state_store(); tasks = s.list_tasks('wf-project-0929-01')
for n in ('test', 'review'):
    print(n, dd.candidate_branch_for_node(tasks, 'wf-project-0929-01', n,
                                          ['implementation'], delivered_in_base=True))
"
# 修复前：agent/opencode/feat-impl-t7-integration-gates-r2  (origin 无 → exit 2)
# 修复后：None  (落在 base agent/gemini-init @ 5d3d615e0)
```

**反向验证**：`test_does_not_fall_back_to_own_node_branch` 使用真实形态分支名
（`agent/pi/test-test-unified-task-workbench-v1-r6`）才使该断言真正有效 ——
首版夹具用含 `...` 的假名被 `sanitize_branch_name` 过滤，测试假通过，
被真实数据复算（仍返回 r6 分支）当场暴露。

### 相关文档 / 关联证据
- `herdr/direct_dispatch.py::candidate_branch_for_node` — 新增 `delivered_in_base`
- `services/herdr-controller.py` — 传 `delivered_in_base=True`
- `bin/herdr-task:2877-2900` — onto 的 origin 校验（fix-loop 语义，未改）
- `tests/test_dispatch_candidate.py` — 5 条新回归 + 1 条修正为真实形态的夹具

## 111. 工作流图拓扑中节点活跃任务数与阶段完成状态文案错位（2026-10-01）

### 问题背景

在 HAFlow Web 控制台（`http://127.0.0.1:8765/`）查看已结束或已合入的工作流（如 `wf-project-0929-01`）时，界面出现两个明显的显示不准问题：
1. 阶段节点卡片底部统计虚标，显示「1 运行 · 完成 1」或「2 运行 · 完成 0」，即使实际没有任何智能体在运行；
2. 已经完成的阶段节点徽章被标注为「待收尾」，与用户预期和实际阶段闭环状态严重冲突。

### 根因分析

1. **活跃任务数采用了粗暴减法推导**：
   在 `herdr/workflow_graph.py:152` 中，`active` 计算为 `active = len(live) - completed`。
   `live` 集合包含节点下所有未被取代（non-superseded）的任务。当阶段内存在历史失败（`failed`）或门禁阻塞（`blocked`）任务时，这些终态任务既不是 `completed`，但也绝非 `active`。粗暴减法导致失败与阻塞任务全部被误算为活跃任务。
2. **节点聚合状态直接复用了任务生命周期字典**：
   阶段节点在聚合完成后的内部状态是 `"completed"`。但在 `console/herdr_factory_console.py` 中，渲染阶段徽章与侧边详情栏直接使用了任务级别的 `humanStatus` 字典。在任务视角下，`completed` 表示“智能体已交付产物，等待 Git 提交/集成/收尾”，因此显示为「待收尾」；而在阶段节点（Milestone）视角下，它代表该阶段已全部就绪闭环，应为「已完成」。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 用全集减单一子集推导活跃状态 | 状态集合不是二元的（有 working/dispatched/failed/blocked/rework 等），粗减法必漏分类 | 活跃项统计必须使用显式状态白名单（`WORKING_LIKE` 或 `rework`），严禁使用反向补集推导 |
| 跨语义实体共用状态展示映射字典 | 任务（Task）与阶段节点（Node）虽然共用某些枚举词（如 completed），但面向用户的生命周期语义不同 | 前端展示区分实体层级，节点级使用专用 `humanNodeStatus`，避免展示语义混淆 |

### 操作规范（已固化到源码与回归）

1. **`herdr/workflow_graph.py`**：
   `active = sum(1 for t in live if str(t.get("status")) in WORKING_LIKE or str(t.get("status")) == "rework")`
2. **`console/herdr_factory_console.py`**：
   定义 `humanNodeStatus(s)` 将节点 `completed` 映射为「已完成」，并在卡片和检查器中统一调用。
3. **回归测试**：
   - `tests/test_workflow_graph_projection.py::test_failed_task_is_not_counted_as_active`
   - `tests/test_console_flow_workbench.py::test_node_status_human_label_completed`

## 110A. 评估命令的重定向和目录隔离必须覆盖整个步骤（2026-09-30）

### 问题背景
`wf-project-0929-01` 的评估入口允许 `cd ... && ...` 等复合命令。模板直接拼接 `> log 2>&1`，只重定向最后一条简单命令；前半段输出丢在runner stdout，cd影响lint，exit可跳过后续检查。

### 经验教训
命令字符串不是单条可执行文件。步骤边界应包围整个脚本片段，捕获完整输出和真实退出码，并隔离该步骤的shell状态；不能靠解析最后一段日志弥补执行边界缺失。

### 操作规范与防护
`herdr/evaluator.py:init_loop` 为test/lint/repro分别生成子shell，再从外层记录退出码。命令内容不改写；失败步骤不会隐藏后续检查。`tests/test_evaluator_step_isolation.py`以真实Bash验证三步复合命令日志、cwd和exit边界。外层runner失败和缺回执另属C28，本项不声明解决。

### 验证与关联证据
`python3.13 -m pytest -q tests/test_evaluator_step_isolation.py`：同一断言修前及撤销修复均5 failed，修后5 passed。相邻专项53 passed/10 subtests，全量2618 passed/145 subtests（372.30s）。仅本地验证，未部署；详见本轮执行计划C06。

## 111. 评估执行完整性不能由绿色测试摘要替代（2026-09-30）

### 问题背景
`wf-project-0929-01` 的exit124/绿色4015摘要先由C01修复；继续追踪完整runner→回执→日志→metrics→EVAL_DONE链，隔离矩阵又发现外层exit17、缺lint/repro、重复回执、旧日志仍可100分收敛，无效回执则抛异常。不能继续只补得分分支。

### 经验教训
实际测试计数、步骤执行结果、整体评估完成是不同事实。外层退出码被忽略，缺失质量/复现回执默认0，日志文件存在不证明属于本轮，这些机制共同允许不完整执行冒充成功。失败也必须进入求助单，否则拒绝收敛后仍无法自主仲裁。

### 操作规范与防护
`bin/herdr-loop`校验原生runner退出、每个所需步骤唯一且有效的退出回执、可读的新写日志；复现配置复用程序生成GOAL。`calculate_metrics/is_converged`同时拒绝完整性失败，保留实际已观察测试数与历史lint基线语义。程序生成错误标签进入原子EVAL_DONE和BLOCKER，错误原值不落盘；旧日志保留但不复用。单次执行完整性还需要生产者所有权：eval/init/基线写入复用既有内核文件锁，持锁覆盖读、执行和快照写入；竞争者busy退出75，不修改共享产物。异常或持有者进程退出释放锁，正常对照可再次评估。锁不等于后代进程清理，C27仍独立处理。

### 验证与关联证据
`python3.13 -m pytest -q tests/test_evaluator_runner_contract.py`：恢复旧runner及指标实现18 failed/2正常对照passed，修后20 passed；真实shell及持久快照、耗尽求助单与缓存满分否决覆盖。相邻76 passed/10 subtests；全量2638 passed/145 subtests（366.56s）。仅本地验证，未部署，历史错误成功记录未重写。相关源码`bin/herdr-loop:run_evaluation`、`herdr/evaluator.py:calculate_metrics,is_converged,generate_blocker_report`。

C29追加验证：`tests/test_evaluator_process_isolation.py`使用独立进程与就绪屏障；撤销锁后3 failed/2 passed，修后5 passed，专项43 passed，全量2643 passed/145 subtests（336.77s）。真实CLI→执行→日志/基线→持久快照，竞争eval/init/baseline均不改持有者产物；异常与进程退出后的恢复通过。仅本地验证，未部署。

C31追加验证：初始化在替换输入前原子失效当前EVAL_DONE；旧原始快照以SHA256归档至history/EVAL_DONE-<sha>.json，历史日志与BLOCKER保留但不作为本轮事实。证据ID绑定读取的单份快照SHA，避免重置后相同计数/iteration复用旧身份；同字节跨进程重启仍去重，未传SHA的旧API保持兼容。升级前后同一旧快照可能被重新观察一次，部署需核对既有ledger；不宣称此SHA证明Task/run归属。撤销关键实现4 failed/1正常对照passed，修后5靶向passed；专项80 passed/10 subtests，全量2673 passed/145 subtests、0 failed/0 skipped（385.00s）。失败初始化也不能留下旧绿证据。仅自审、未部署，C03c恢复epoch和C08业务交付仍未关闭。

C28c补充（2026-10-01）：

**问题背景**：缺linter返回127，fallback解析为1错误并写baseline；以后仍127时差值0，原子回执converged=true。执行失败被误当成历史诊断欠账。

**经验教训**：baseline只能抵扣实际静态诊断；工具无法执行、超时和信号终止不能成为可抵扣债务。仅拒绝新baseline不足，旧版本已污染的baseline也必须在metrics与收敛边界独立否决。

**操作规范与防护**：共享reserved退出分类124/126/127、负值与128以上；capture拒绝发布假债务，评分与is_converged独立veto。实际exit1及TypeScript exit2仍保留既有差值门禁，不把所有非零当新缺陷。Task既有best-effort告警保留，无live数据修复。

**验证与关联证据**：`tests/test_lint_execution_failure_gate.py`包含lint/type失败矩阵、原生CLI缺工具/不可执行、Task告警、旧污染baseline实际持久回执、正常exit1/2。扩展旧实现19 failed/3正常对照；最终89专项/10子测试通过；全量2718 passed/145子测试，0失败/0跳过（387.92s）；仅本地验证未部署，提交结果见本轮计划C28c。任意工具自定义低位配置退出码仍需其具体契约，不用本卡宣称所有配置失败均分类。

C31b补充（2026-10-01）：

**问题背景**：current评估回执先替换，再mkdir/write/replace历史；任一故障后重试只能看到reset字节，原始receipt永久丢失。

**经验教训**：失效旧事实前必须先建立可恢复历史；归档失败时仍未发布新契约，保留旧契约对应的current是合法旧事实，不能先把证据抹掉。

**操作规范与防护**：同一内核锁内先内容寻址归档并校验已有内容，再原子reset current，最后发布新输入。历史冲突立即拒绝且current不动，重复同内容幂等。保持初始化失败不把旧绿证据用于新输入的原有门禁。不宣称文件replace提供掉电持久性。

**验证与关联证据**：`tests/test_loop_history_publication.py`含实际成功/耗尽回执、mkdir/write/replace/SystemExit、native CLI文件系统障碍与重试、同SHA冲突/正常复用。旧及撤销11 failed/1正常，修后12 passed；91相邻/10子测试通过；全量2730 passed/145子测试，0失败/0跳过（360.26s）；详见计划C31b。仅local未部署，C31c历史BLOCKER过滤独立待修。

C31c补充（2026-10-01）：

**问题背景**：extract读取原子EVAL_DONE，但collect_execution_evidence→summarize_loop仍从STATE/METRICS和BLOCKER存在性拼装事实；重置后遗留BLOCKER、显示文件写入故障或更新交错可把旧事实送入监督器。

**经验教训**：旁路摘要也必须遵守相同权威来源；不能只修主读取器或只隐藏BLOCKER标记。程序读取的一份原子receipt定义当前状态/计数，历史文件存在不等于当前阻塞。

**操作规范与防护**：共享best-effort单次原子reader；modern摘要仅同份receipt，current exhausted才报告BLOCKER存在。坏/薄/过深/不可读receipt返回unknown，不回退旧显示。仅absent receipt保留legacy读取契约，明确不保证legacy跨文件原子性。保留历史BLOCKER及显示文件，不声明SHA证明run归属。

**验证与关联证据**：`tests/test_loop_current_summary.py`12例：真实eval/reset/collector、中途METRICS写失败、单次read后replace交错、legacy及异常解析。旧最终10 failed/2正常，106相邻通过；全量2742 passed/145子测试，0失败/0跳过（370.78s），详见计划C31c。首次2项测试对init行为假设错误已纠正，日志保留，不当产品失败证据。

C34补充（2026-10-01）：

**问题背景**：全量中确定性Observer gateway测试drain10秒后finding为空，但之后数据库出现finding。前序unittest设置假key，cleanup只恢复原有变量，未删除本来absent的新key，默认启用Jev使确定性测试走外部模型路径。

**经验教训**：凭据生命周期必须精确恢复absence和值；启用旁路诊断不意味着测试应启用模型。单独通过不能证明全量隔离；必须保留失败与迟到持久化证据，不延长timeout遮盖错误依赖。

**操作规范与防护**：修正原helper cleanup，suite每例移除继承Jev凭据并默认禁用Observer模型；确定性gateway明确provider disabled和禁止transport。真实provider错误测试明确opt-in受控loopback，原断言不放宽。生产模块无改动。

**验证与关联证据**：`tests/test_model_test_environment_isolation.py`执行真实unittest.run cleanup absent/present及实际gateway零transport；旧/反证2 failed/1正常，higher judge_many与inner transport各自计数避免spy覆盖/吞异常盲点；最终字节移除禁用保护1 failed/2正常，恢复3 passed；模型/Observer119及扩大155专项通过；全量2745 passed/145子测试，0失败/0跳过（405.16s），详见计划C34。此前外部请求是否发生未直接证明，只证明默认模型路径和key泄漏，不冒称无出站；受控复现无实际请求。C15b代码独立暂存另验。

C15b补充（2026-10-01）：

**问题背景**：ANSI颜色前缀位于绿色✓/√之前，startswith漏识别通过标记；用例名含FAIL/✕被误列失败，绿色套件降到95而不收敛。

**经验教训**：终端装饰不是测试语义，解析边界须复用已有清洗后判定，不改评分掩盖，也不补无限颜色marker。

**操作规范与防护**：test parser复用projection.strip_ansi_codes，仅规范化内存视图；原始日志、真实exit和实际失败门禁不变。C34测试隔离修复单独提交后重新验收，不把旧失败全量称通过。

**验证与关联证据**：`tests/test_evaluator_ansi_test_output.py`13例含Vitest/Jest、✓/√、FAIL/✕名、真失败、非零exit、明文对照与实际runner原字节/回执。新基线13pass，撤销11 failed/2正常；全量2758 passed/145子测试，0失败/0跳过（385.08s），详见计划C15b，未部署。

C28b补充（2026-10-01）：

**问题背景**：reader扫描整个GOAL是否出现repro字段，自由目标/验收示例误阻塞绿色评估；最初修复只取最后配置区块，又被合法多行repro字面量中的完整伪区块骗成false。

**经验教训**：自由文本不能充当可执行配置；没有结构化边界的旧Markdown无法可靠消歧，不能为了兼容猜最后一块，更不能把不确定性变成成功。

**操作规范与防护**：现有GOAL末尾附program生成typed requirement，来自真实repro_cmd，置于全部原始字段之后，不新建平行事实文件。新reader标记优先；legacy仅唯一完整单行配置，其余unknown/error需重新初始化。实际命令执行和fresh receipt/log完整性门禁保留。

**验证与关联证据**：`tests/test_goal_repro_configuration.py`22例含自由字段/typed伪标记、多行命令、legacy兼容/歧义、原生CLI roundtrip、真实repro漏runner和合法Bash literal正向/反向。中间legacy漏洞完整回归1失败；最终22pass/核心撤销19fail3正常；89专项/10子测试通过，全量见计划C28b，未部署。

## 112. 评估超时必须回收本次创建的进程组（2026-09-30）

### 问题背景
`wf-project-0929-01` 的评估出现exit124。只超时终止直接shell不能终止npm/node等后代；隔离就绪屏障确认超时后子进程仍会继续写文件，正常shell返回也可能留下后台进程。

### 经验教训
进程退出和工作结束不是同一事实。共享产物所有权只能在本次执行的后代停止后释放；不得按进程名称寻找或清理无关工作。宿主对已消失的进程组可能返回EPERM，必须核对实际存活成员，不能吞掉活进程的权限拒绝。

### 操作规范与防护
`herdr/evaluator.py:run_evaluation_command`在新session启动本次命令，供`bin/herdr-loop`与Task的lint基线采集复用；超时、中断、异常和正常返回都清理其进程组，TERM后有限等待，残留成员用KILL，保留原生超时124和已观察回执。主线程的SIGTERM处理仅在受管命令作用域内转为栈退出并恢复原handler。`capture_lint_baseline`持锁覆盖命令和基线写入，超时不写伪造基线；已有债务解析不变。只忽略经ps成功核实为空/僵尸的EPERM；活进程拒绝仍为失败。不可捕获的SIGKILL以及主动脱离session的子进程不在此保证内，需Supervisor现场处理；本项没有清理生产进程。

### 验证与关联证据
`tests/test_evaluator_process_cleanup.py`用真实独立CLI、shell、Python子进程和就绪屏障验证超时、拒绝TERM、SIGINT/SIGTERM、后台残留及无关进程存活；正常前台执行保留通过。旧实现核心矩阵5 failed/1 passed，修后7 passed（含活进程拒绝不能忽略的专项）。相邻45 passed，全量2650 passed/145 subtests（390.39s）。仅本地验证、未部署；详见执行计划C27。

C27b追加验证：实际auto-init基线入口旧4 failed/1正常对照passed，修后5 passed；相邻39 passed，全量2657 passed/145 subtests（360.47s）。覆盖超时（含宽限期后强制回收）、SIGTERM中断、并发初始化拒绝、基线债务保留和锁恢复。采集脚本保持原`/bin/sh -c`语义。初次夹具导入失败与宽限期内已完成子进程的观察保留，随后用超过宽限期的受控子进程验证强制回收；不放宽存活/产物断言。原C27记录对应ec334bb，公共执行入口在C27b归入evaluator。仅本地验证，未部署。

### 2026-10-01 复核补证：公开初始化入口必须持锁到基线发布

**问题背景**：独立审查发现原生CLI在init_loop释放锁后仍使用旧subprocess.run采集基线，竞争init可改契约，旧采集随后覆盖新baseline；竞争eval甚至可使用上一契约的债务假绿。SIGTERM只结束CLI，后代晚写仍可发生。Task两段各自持锁也留下初始化/采集间隙。

**经验教训**：单个helper持锁和整个操作持有所有权不同。基线属于本次契约，重新init必须先让旧债务不可用，命令及发布结束后才允许下一生产者。

**操作规范与防护**：init_loop的显式capture_baseline在同一次内核锁内覆盖初始化、旧基线失效、共享受管命令、发布；Task和原生CLI均启用。内部unlocked capture避免再次加锁，standalone capture继续自行持锁。超时不保留旧baseline；CLI失败不返回成功，Task保留既有告警/启动行为。合法exit1已有债务仍可采集；missing-linter退出语义由C28c单独处理。

**验证与证据**：原生进程竞争init/eval、SIGTERM含忽略TERM后代、真实CLI受控超时、Task首锁释放状态、旧债务失效与正常债务对照；撤销三producer/core文件8 failed/1正常对照，修后9靶向、86相邻/10子测试、全量2696 passed/145 subtests（384.91s）。两CLI AST/compileall/help/diff-check通过，独立复审无此项阻断，未部署。证据为执行计划C27c与test_loop_init_baseline_atomicity.py；SIGKILL/主动脱离session仍非保证范围。

## 113. 自动测试命令必须声明非交互环境（2026-09-30）

### 问题背景
`test-r4`的4015条绿色摘要后出现`PASS Waiting for file changes`并exit124。`auto_init_task_loop`仅据package.json生成`npm test`，继承Agent的TTY输入，Vitest默认进入watch，导致任务反复耗尽。

### 经验教训
绿色摘要不证明命令已结束。普通管道能退出也不能排除TTY下的挂起：当前Vitest默认watch取决于非CI与stdin.isTTY，验证必须保留真实触发条件，不能只做无TTY对照。

### 操作规范与防护
自动npm默认命令改为`CI=1 npm test`，GOAL和脚本保持同一配置；显式`--test-cmd`完整保留，不擅自改变任务范围。此修复仅覆盖支持CI语义的默认npm命令；多栈仓库Java任务误选根前端测试仍属C05b，不能以非交互退出代替正确测试契约。

### 验证与关联证据
真实Task自动初始化→npm→Python测试脚本→eval→持久快照，同断言旧1 failed/1正常对照passed，修后2 passed；相邻18 passed，全量2652 passed/145 subtests（381.91s）。另用已安装Vitest 3.2.6和真实PTY输入验证旧命令1 passed后exit124、不收敛，修后1 passed且exit0收敛；无TTY旧命令正常退出，保留为触发条件对照。仅本地验证、未部署。源码`bin/herdr-task:auto_init_task_loop`，回归`tests/test_task_loop_noninteractive.py`。

## 114. Agent 活性信号不能代替内循环仲裁决策（2026-09-30）

### 问题背景
`wf-project-0929-01`的blocked/working来回切换会让待投递仲裁事件变成stale。Controller实时`handle_event`与重启`reconcile_task_state`均只凭Agent working/done把blocked改回working，覆盖程序记录的耗尽事实。

### 经验教训
活性与业务决策是不同事实。等待仲裁不等于Agent进程停止；普通运行信号不能解除待决阻塞。旧`sentinel_reason`又可能在后续普通阻塞中保留，直接按该字段保护所有blocked会制造另一个无法恢复的卡点，必须以本次状态转换历史为准。

### 操作规范与防护
`blocked_event_type`优先读取最新blocked转换的明确reason，缺历史时保留既有legacy fallback。当前内循环blocked拒绝普通working/idle/done解除；重启遇到已知运行信号则恢复仲裁队列，不改变状态。显式合法恢复/返工以及新的普通blocked保持原行为。旧屏幕标记在显式恢复后的重复采样仍为C03c；仲裁卡和人工升级提示必须使用现有合法blocked→working恢复命令；没有扩展状态机，没有force放行。命令验证独立于运行信号保护（C30）。

### 验证与关联证据
`tests/test_inner_loop_arbitration_recovery.py`用真实临时SQLite观察→CAS→运行事件/重启→队列→通知出口验证，外部输送替换但持久转换不替换。撤销实现6 failed/2正常对照passed；修后8 passed，相邻60 passed，全量2665 passed/145 subtests（352.76s）。初版对照采用了仲裁卡上的非法直接rework，4项报InvalidTransitionError；修正对照为合法显式恢复后再返工，并保留该协议缺陷为C30。仅本地验证、未部署，无独立评审。

C30追加验证：`tests/test_blocked_recovery_command_contract.py`提取真实提示的CLI参数，只将程序路径重定向到隔离Candidate，在临时SQLite实际执行set并读取状态/历史；恢复命令旧2 failed/1失败策略对照passed，修后3 passed。相邻47 passed，全量2668 passed/145 subtests（358.35s）。原提示文本测试改为断言确切`set wf-1-impl-x working`，新真实执行断言未放宽。只替换通知出口，未向任何人实际发送升级消息；路径版本固定仍属C20，未部署。

## 113. 阻塞队列必须属于一次转换，状态名不代表同一事件

**现象与影响**：耗尽事件排队等待期间，显式恢复后再次阻塞，旧事件仍因当前status=blocked被投递。相同event_type去重还会抑制新阻塞，旧消费finally可能清掉新事件所有权。

**根因与证据**：enqueue只带task/event，消费仅检查状态名。真实TEMP SQLite恢复→再次阻塞、不同Run、同timestamp及busy等待交错中，旧实现最终9失败/1正常对照。普通metadata save会增加版本，单纯版本相等又会误丢同一次阻塞。

**修复与预防**：复用持久workflow/run/status_history长度与最新转换生成episode；无history旧行保守绑定版本。blocked类队列携带并逐轮核对episode和当前cause，去重queue_key独立带episode；attention沿用原key，不改变既有重试存储。旧消费只释放自己queue_key。构造消息后、发送前再读核对；未知legacy版本变更丢旧权威时，安全补排当前持久blocked，不能因保守判未知造成永久漏仲裁。

**验证与关联证据**：tests/test_blocker_queue_episode.py 10项全部通过，最终反证9失败/1正常；相邻结果见计划；全量见计划C21b。仅本地代码，未部署。外部prompt与数据库转换不属于同一事务，不能声称此修复消除最后一次读到发送之间所有并发窗口；历史事实仍需保留，未知legacy不能凭状态名推断连续性。

## 114. 候选身份不能用相对基线新增提交数替代

**现象与影响**：已冻结候选恰好在base或已被合流时，rev-list base..onto为零，Controller反复拒绝后续测试/评审派发；既有日志出现10次候选空差提示。

**根因与证据**：差异数只说明分支关系，不说明该提交是否为已确认待验证候选。真实TEMP Git和SQLite冻结事实经实际Controller入口到Mock TaskCLI，原版最终3失败/12正常对照；正常冻结base、latest轮换回同SHA和双onto合法批次均被拒。

**修复与预防**：仅在已有零差判断时复用最新冻结台账，要求严格完整40位SHA、每条spec有onto且pin完全一致，再逐onto native commit与frozen匹配。初版遗漏无onto同批spec，独立评审证明会启动无pin Task，回归后收紧all；不改非空与未知Git既有策略，不删除空分支保护。

**验证与关联证据**：tests/test_frozen_base_candidate_dispatch.py 15项，通过真实冻结轮换/跨workflow隔离/短pin/旧spec/onto不匹配/混批缺身份/双onto正例，154扩展相邻和35子测试通过，全量见C13计划；旧fixture接线14项TypeError单行兼容修正，未放宽断言。仅本地未部署，TaskCLI/Worker现有复验仍需执行；冻结读取与外部launch非原子，不用本卡冒充所有并发原子保证。

## 115. 永久能力拒绝必须退出重试，非空失败对象不是送达

**现象与影响**：soft_steer_not_supported曾写2881次失败，每轮保留pending继续尝试；Sentinel判断非空返回对象而打印Injected，CLI与Console反馈也把失败混成成功队列。

**根因与证据**：声明能力不支持属于永久失败，不会因等待idle恢复。dispatch_pending_steer却用同一pending表示暂时无pane、暂时送达失败、永久能力拒绝；上游用对象truthiness代替ok。真实TEMP SQLite、Native CLI、Sentinel两轮与Node执行实际submitSteer中，撤回4核心实现9失败/5正常。

**修复与预防**：新请求先检查既有能力，不支持保存blocked与原文/原因/count0；legacy pending首次拒绝后blocked并保留唯一失败history，后续soft扫描不再选择。暂时失败继续pending。CLI失败非零、Console明确失败、Sentinel仅ok才记录Injected；不修改Agent能力、不将软指令自动升级打断。

**验证与关联证据**：tests/test_steering_permanent_failure.py 14项通过，62扩展相邻通过，全量见C02计划；已有unsupported测试更新为更强立即拒绝/保留原文/0TTY断言。内部同ID显式urgent恢复只有受控Mock外部边界证据；公开CLI/Console急送创建新指令，不能宣称原ID公开自动恢复。没有真实Agent调用，没有处理live历史队列，暂时失败与跨Run语义各自保留边界。


## 116. Git 锁等待不等于失败预算，错误文本不证明锁身份

**现象与影响**：已完成T8因index.lock存在连续提交失败，普通finalize预算耗尽后持久升级，即使外部锁后来消失也不再自动推进。

**根因与证据**：native add的锁冲突未分类，CalledProcessError作为通用提交错误；重试驱动不区分等待和质量失败。临时原生普通/linked Git、真实CLI/SQLite与Controller复现；同时独立审查证明hook和clean filter可输出精确fatal文本而没有实际外来锁，不能仅凭stderr免预算。

**修复与预防**：仅命令前后稳定实际native index锁身份+精确原生错误给结构wait/rc75。专属wait不增加普通错误次数，持久60秒退避；以Run/持久转换episode识别当前完成周期，既有EpisodeStore文件锁CAS保护等待写入和旧owner失效，metadata保存不抹错误预算。不删除/移动锁，不自动翻案legacy通用升级。

**验证与关联证据**：tests/test_finalize_git_index_wait.py 25项；真实受控TEMP锁超过5轮不耗预算、锁/HEAD/index/tracked/untracked保留，夹具释放锁后实际Controller在due前不动、due后通过真实CLI将原Task提交；integration用受控busy替身，非真实集成验收。包含独立进程attention竞争、两个stderr伪装、GIT_INDEX_FILE、历史新owner与metadata对照。最终Controller反证10失败15正常；相邻/full见C35a计划。实盘锁创建者未知，当前升级仍需单独安全恢复；两个持久存储和外部Git没有共同事务，不承诺最后读后所有竞态已消除。


## 117. 省略已交付任务分支不能省略验收候选身份

**现象与影响**：已冻结、实现已交付而delivery note尚未形成的正常窗口，test/review省略onto后也丢失候选SHA，CLI以delivery_missing拒绝，自动验收不能推进。

**根因与证据**：main125正确省略未推送任务分支，但候选resolver把branch缺失当作pin缺失；旧C13 fixture模拟了带pin计划。恢复真实selector/planner后实际CLI preflight两项失败，六项正常对照保留。

**修复与预防**：只在无effective delivery、candidate_branch=None、当前workflow最新严格40位冻结SHA与native source HEAD exact相同才保留pin。冻结身份与业务delivery是不同事实；不恢复onto、不放宽TaskCLI/Worker复验。

**验证与关联证据**：tests/test_delivered_base_frozen_pin.py八例，撤销2失败6正常，40相邻及3子测试通过；全量结果见C13b计划。实际CLI身份方法被执行，完整Worker后段仍由既有baseline回归验证；无真实Agent/模型调用，未伪造delivery。


C13b最终：66相邻passed/3子测试（32.93s）；最新main4cca57e合并后完整全量2871 passed、154 subtests passed、2 skipped（隔离HOME无LaunchAgent），0 failed，454.85s。两项本机只读plist检查另行2 passed（0.07s）。compileall、三入口CLI AST、diff-check通过；独立最终只读复审组合阻断闭合，未自行重跑全量。真实Agent/Worker启动、业务E2E及开放卡未因此验收。

## 118. 已结束工作流在画布投影与阶段聚合中的终态穿透与门禁放行感知（2026-10-01）

### 问题背景

在 HAFlow Web 控制台查看已结束的工作流（如 `wf-project-0929-01`，其在数据库中已执行 `close-workflow` 标记为 `status: completed`，且 `gate_overrides` 中对实现和测试阶段均记录了 `verdict: pass` 的人工放行）：
1. 界面画布（Canvas）上，「实现」仍显示红色「失败」，「测试」仍显示红色「已阻塞」，「收尾」仍显示灰色「等待」（底部文案「尚未开始」）；
2. 调度驾驶舱「当前关注阶段」仍指向失败的「实现」阶段；
3. 用户反馈：「有变化 但是不全对 这个工作流已经都结束了」。

### 根因分析

1. **图拓扑纯投影函数忽略工作流终态与门禁裁决**：
   `herdr/workflow_graph.py:workflow_graph_projection` 仅接收规范化节点定义与任务列表，完全没有消费工作流主记录的 `status` 与 `gate_overrides`。即使工作流整体已关闭（`completed`），投影引擎仍然逐个节点按底层任务状态盲目聚合。
2. **任务聚合状态存在单次失败永久污染缺陷**：
   `aggregate_node_status` 原逻辑使用 `if any(s == 'failed'): return 'failed'`。在真实软件工程流水线中，节点经历重试、修复任务成功（或人工 force-pass 裁决）后，历史上被记录为 `failed` 的旧任务未被清除，导致即使最新任务成功或门禁放行，该阶段依然被永久定格为 `failed`。
3. **控制台详情组装未向投影层透传运行时状态**：
   `console/herdr_factory_console.py:workflow_graph_for` 仅传 `definition`，丢弃了 `workflow.status` 与 `gate_overrides`；同时 `stage_summary` 亦未感知工作流终态与放行裁决。
4. **前端卡片底部文案缺少完成态分支**：
   `flowCardFoot(n)` 在 `!n.task_count` 时无条件返回「尚未开始」，导致没有任务直接伴随工作流收尾关闭的「收尾」阶段在已完成时仍显示「尚未开始」。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 投影视图仅从叶子任务自底向上聚合，忽略顶层实体终态 | 整体生命周期终态（Workflow completed）高于局部历史重试状态；自底向上聚合容易被历史脏事实误导 | 当父容器进入终态（completed/cleaned/archived）时，所有子视图节点状态强制对齐终态，清零活跃与需关注标记 |
| 阶段聚合以 `any(s == 'failed')` 判定失败 | 失败是单次尝试的过程事实，最新成功（或 gate pass）才是当前结论 | 聚合任务状态时，若无活跃运行中任务，应优先检查 gate_verdict pass 与按时序排序的最新任务完成态 |
| 前端对空任务状态盲目假设「未开始」 | 空任务可能是尚未调度，也可能是随流程跳过或直接核准的终态 | 展示文案必须结合实体 `status` 综合判定，`n.status === 'completed'` 时必须显示「已完成」 |

### 操作规范（已固化到源码与回归）

1. **`herdr/workflow_graph.py`**：
   - `workflow_graph_projection` 读取 `workflow.status` 与 `gate_overrides`。若工作流为 `completed/cleaned/archived`，节点状态一律置为 `completed`，`active_task_count = 0`，`has_attention = False`；若存在 `verdict: pass` 放行且无活跃任务，置为 `completed`。
   - `aggregate_node_status` 在无活跃任务时按最新时序任务与 verdict pass 判定完成。
2. **`console/herdr_factory_console.py`**：
   - `workflow_graph_for` 合并透传 `status` 与 `gate_overrides`。
   - `stage_summary` 接收 `workflow` 参数，对齐已完成与放行阶段判定。
   - `flowCardFoot` 在 `status === 'completed'` 时显示「已完成」。
3. **回归测试**：
   - `tests/test_workflow_graph_projection.py::TestCompletedWorkflowAndGateOverrides`
   - `tests/test_console_stage_summary.py::test_completed_workflow_makes_stage_cleaned`

## 119. 全局通用 CSS 类名碰撞与组件状态徽标隔离防护（2026-10-01）

### 铁证与现象

在实现控制台 Linear 风格下拉框标准组件时，用户截屏反馈徽标异常：
1. 徽标胶囊被压扁为 7px × 7px 的椭圆细环；
2. 胶囊文字（`3 活`、`4 等`）被挤出胶囊外，并发生单字纵向竖排折行；
3. 用户反馈：「后面的数字暂时太丑了 优化一下」。

### 根因分析

1. **全局泛化类名冲突（Global CSS Selector Collision）**：
   在控制台单文件体系（`console/herdr_factory_console.py:2479`）中，历史定义了用于表格与弹窗状态指示器的全局样式：
   `.dot { width: 7px; height: 7px; border-radius: 50%; }`。
   下拉框徽标使用了复合类名 `<span class="badge-pill active dot">`，命中了该全局规则，导致原本应为自适应宽度的胶囊被强制锁定为 7px 宽 × 7px 高。
2. **文字防折行与紧凑度缺失**：
   未设置 `white-space: nowrap` 与 `flex-shrink: 0`，导致外溢文本在父级 flex 排版下逐字折行。
3. **文案规范随意**：
   使用了「3 活」「4 等」这类口语化缩写，与控制台「活跃」「等你的问题」专业语义脱节。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 泛化类名冲突（`.dot`）压扁组件 | 在原生 CSS/无构建单文件项目中，禁止使用通用短单词作为修饰类（modifier） | 使用伪元素 `.badge-pill::before` 绘制点状指示器，不得给宿主增加全局命名冲突类名 |
| 数字与状态文案竖排折行 | 指标与数字胶囊在任何缩放与弹性布局下绝对不可单字折行 | 胶囊必须显式指定 `white-space: nowrap; flex-shrink: 0; font-variant-numeric: tabular-nums` |
| 口语化简写损害工业级品质 | 随意缩略（如「活」「等」）让界面显得廉价、含义模糊 | 统一为标准业务语义（`• X 活跃`、`• Y 需决策`、`已完成`、`聚合`） |

### 操作规范（已固化到源码与回归）

1. **`console/herdr_factory_console.py` & `console/static/prototype_workflow_dropdown.html`**：
   - 彻底清除 `.badge-pill.dot`，采用 `.badge-pill::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }` 绘制点状指示器；
   - 聚合等无点徽标使用 `.badge-pill.nodot::before { display: none; }`；
   - 添加 `height: 19px; padding: 0 7px; white-space: nowrap; flex-shrink: 0; font-variant-numeric: tabular-nums;`；
   - 标题容器与徽标容器补充 `flex: 1; min-width: 0` 与 `white-space: nowrap; flex-shrink: 0`；
   - 文案规范统一为 `${w.active} 活跃` 与 `${w.attention} 需决策`。
2. **自动化门禁测试**：
   - 在 `tests/test_console_linear_dropdown.py` 中固化 `test_badge_pills_styling_and_labels`：
     - `self.assertNotIn(".badge-pill.dot", self.source)`
     - `self.assertIn("tabular-nums", self.source)`
     - `self.assertIn("活跃</span>", self.source)`
     - `self.assertIn("需决策</span>", self.source)`

## 120. 工作流可靠性必须覆盖回执生产者、持久化窗口及真实资源边界（2026-10-02）

### 问题背景

工作流监控后进行六项可靠性改进。独立审查以真实 CLI、临时 Git/SQLite 和进程交错复现：完美的测试夹具掩盖真实 EVAL_DONE 缺少候选/执行身份；凭据轮换与提示发送之间的崩溃可能留下永远收不到提示的任务；绿色测试摘要也不能证明当前候选或生产环境已验收。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 读取层测试手工填入生产者从未生成的字段 | 测试对象必须包含真实生产者 | 至少验证 CLI→核心→SQLite→Controller→报告的实际链，未知身份不能用当前任务补填 |
| 本地落库后跨原生传输发生中断 | 数据库事务不能提供外部发送的 exactly-once | 持久准备、发送开始、发送回执分阶段；未开始可恢复，已开始未确认保留 unknown |
| 运行版本从目录名推断，关闭重复调用只做顺序测试 | 配置和标签不是执行事实 | 校验 release 内容；关闭用跨进程所有权与逐资源日志，资源身份未知时保留 |
| 子进程继承数据库路径、私有文件被误当作跨工位沙箱 | 可用性修复不能扩大权限或夸大隔离 | 工具环境移除数据库/投影路径；明确同 UID 信任边界；凭据由服务端限定有效期 |

### 操作规范（已固化到源码与回归）

1. `completion_receipt` 绑定 task/run/epoch、服务端有效期、既有执行门禁及 CAS；终端文本只用于无协议的历史任务。
2. `task_checkpoint` 重新验证受管分段的内容与归属；`bounded_tools` 限制输入/输出/超时，保留不确定副作用。
3. `evaluation_identity` 从实际执行前后源码与步骤退出码生成事实；`delivery_report` 区分观察结果、候选验收及生产验证。
4. `workflow_close` 使用跨进程生命周期锁、操作日志与实例身份；没有原生 API 能力时不伪造回收成功。

### 验证命令 / 守护测试

```bash
pytest -q tests/test_completion_receipt.py tests/test_completion_expiry.py tests/test_evaluation_delivery_chain.py
pytest -q tests/test_dispatch_idempotency.py tests/test_task_checkpoint.py tests/test_bounded_tools.py
pytest -q tests/test_service_release.py tests/test_workflow_close_claim.py tests/test_close_workflow_receipt_cli.py
```

预期：专项通过且负向用例保持未知/拒绝，不写生产状态或启动真实 Agent。全量结果以当前源码冻结后的交付记录为准，本条不声明已部署。

### 相关文档 / 关联证据

- `docs/superpowers/specs/2026-10-02-workflow-reliability-design.md`
- `tests/test_evaluation_delivery_chain.py` — 实际 loop/Controller/验收投影
- `tests/test_completion_expiry.py` — 服务端过期与缺失身份拒绝
- `tests/test_workflow_close_claim.py` — 独立进程关闭与恢复

---

## 121. macOS 系统 Bash 的 nounset 空数组必须走实际安装路径验证（2026-10-02）

### 问题背景

813164f 的本地发布在全部服务迁移到快照后，系统 /bin/bash 3.2.57 以 KICKSTART_SERVICES[@]: unbound variable 中断；此前 --no-restart 测试没有执行重载。临时 HOME 的真实入口回归进一步复现全工作区与无服务布局的同类失败：3 failed、1 passed。

### 经验教训

set -u 下 Bash 3.2 把空数组展开视为未设置变量；只验证混合布局或 bash -n 无法覆盖运行时问题。兼容修复应保护每个可空数组，不关闭 nounset，不生成空参数，并保留逐参数引用。

### 操作规范

scripts/install-herdr-console.sh 的 plist_paths、SNAPSHOT_SERVICES、KICKSTART_SERVICES 使用条件数组展开。实际 /bin/bash 安装测试覆盖全快照、全工作区、混合、无服务，与重启/--no-restart 的组合，含空格及引号路径。仅替换 launchctl、ps、sleep 外部依赖；真实归档、快照校验、plist 发布与安装入口仍执行，禁止测试触碰本机服务。

### 验证命令 / 关联证据

`pytest -q tests/test_installer_bash_compat.py tests/test_service_release.py tests/test_install_herdr_console_deploy.py`。红绿日志见本任务 .omc/bash3-red.log、bash3-final-subset.log；本记录不声称新的本地部署。

---

## 122. Agent 路由健康检查必须防范 CLI 格式杂音、并发风暴与过度防御导致的死锁（2026-10-02）

### 问题背景

多智能体自动路由在生产现场出现“永远只选 opencode”的单点故障。现场排查揭示三大根因：现代 CLI（如 Qoder、Kimi）输出中带有 ANSI 彩色控制符、Markdown 符号或良性日志横幅，导致 `smoke_response_verified` 判定失败误判为 `UNKNOWN`；预检逻辑以 `len(allowed)`（多达 8 个进程）并发探测拉满 CPU 与网络，导致部分 Agent 超时成为 `TIMEOUT`；路由决策将 `UNKNOWN` 与 `TIMEOUT` 均当做不健康黑名单，且快照过期后仍然无差别硬过滤，导致候选人全被排除，系统永久失去自愈能力。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| CLI 协议演化带来输出格式杂音（ANSI/Markdown/横幅/JSON） | 不能假设所有 Agent 的 CLI 严格输出纯单行 ASCII marker | 证据提取必须剥离 ANSI 颜色码、清理外层 Markdown/标点，放行良性横幅行，并支持通用 JSON 消息提取 |
| 并发探测风暴争抢资源引发虚假超时 | 探针并发度不能与候选节点数量无界绑定 | 探针必须采用受控线程池（默认并发度 4，支持环境变量配置），防范突发并发打崩网络与 CPU |
| 软性故障（TIMEOUT/UNKNOWN）被永久硬过滤 | 探测超时或未知格式不等于不可逆硬故障（如 TOKEN_EXHAUSTED、AUTH_REQUIRED） | 区分硬故障与软故障；快照过期后放行 TIMEOUT 与 UNKNOWN 尝试调度，允许系统自愈；仅硬故障持续隔离 |

### 操作规范

1. `smoke_response_verified` 引入 `_strip_ansi`、`_clean_smoke_token` 及 `_is_benign_banner_line`，区分 JSON 模式与纯文本模式的 prompt echo 检查。
2. `herdr/agent_router.py` 定义 `HARD_UNHEALTHY_STATUSES`，新鲜快照隔离异常；2026-10-03补证：过期软故障只允许进入真实deep请求重验，READY/request_verified/verifiable identity齐备才可选择，不能因TTL失效直接放行执行。定向部分刷新只更新该Agent时间/identity，不将全局快照伪装新鲜。
3. `herdr/deep_preflight.py` 设定 `DEFAULT_PREFLIGHT_CONCURRENCY = 4`，支持 `HERDR_PREFLIGHT_CONCURRENCY` 动态控流。

### 验证命令 / 关联证据

- 单元测试：`pytest -q tests/test_agent_router_preflight.py tests/test_preflight_runtime_contract.py tests/test_deep_preflight_accuracy.py`
- 全量关联测试：`pytest -q tests/test_agent_router_stage_exclusion.py tests/test_adaptive_router.py tests/test_canary_router.py`
- S6 代码审查报告：`.omc/review-80030f57-7a2b-4988-a486-8bc208e1ccb3.md` (MERGE_READY)

---

## 123. 沙盒重置与清理不得破坏前置落盘的私有资源身份标桩（2026-10-02）

### 问题背景

`herdr-task launch` 在 worker 阶段出现新任务派发必崩故障：`FileNotFoundError: '<clone>/.herdr-launch-identity.json'`，伴随 clone 回滚但已建好的 tmux pane 发生泄漏。根因是 `services/herdr-worker.py` 在 `main()` 中先将 `launch_identity` 写入 clone 根目录作为未跟踪文件（`write_worker_launch_identity(..., initial=True)`），随后的 `create_task_branch` / `checkout_onto_branch` 调用 `sanitize_clone_sandbox`，内部裸执行 `git clean -fd` 清理沙盒未跟踪文件，误将身份标桩文件删除；当 worker 执行到 `initial=False` 的回读安全校验时，因原文件丢失触发 `FileNotFoundError` 导致失败。此前曾有临时在 `~/.config/git/ignore` 中加入该文件的非通用绕过。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 沙盒粗暴清理误杀关键运行态标桩 | `git clean -fd` 会无差别清除所有未被 `.gitignore` 保护的未跟踪文件 | 破坏性沙盒清理必须对受管的内部标桩文件（如 `.herdr-launch-identity.json`）显式配置 `-e` exclude 排除保护 |
| 打标与清理顺序颠倒引发状态盲区 | 调整执行顺序（如清理后再打标）会破坏崩溃恢复语义，导致清理期间崩溃时资源无法归属追溯 | 保留“在破坏性操作前先落盘证明归属”的安全顺序；不可随意推迟打标时机 |
| 依赖机器局部配置临时绕过缺陷 | 用户级 `~/.config/git/ignore` 或仓库级 `.gitignore` 临时放行平台私有文件不是真正修复，无法跨机器/CI 泛化且污染业务仓库 | 修法必须收敛在沙盒清理函数内部；测试时强制配置 `core.excludesFile=/dev/null` 隔离全局 ignore，彻底防范假阳性 |

### 操作规范

1. `services/herdr-worker.py` 的 `sanitize_clone_sandbox` 中，在 `git clean -fd` 执行时显式增加 `-e .herdr-launch-identity.json` 参数。
2. 任何涉及 `git clean` 清理沙盒的场景，测试用例必须配置 `git config core.excludesFile /dev/null` 排除宿主全局 git 规则干扰，直接断言受保护文件在清理后存活、其余临时垃圾被正常删除。
3. 移除任何为了绕过沙盒清理而临时写入宿主 `~/.config/git/ignore` 的条目。

### 验证命令 / 关联证据

- 专项回归测试：`pytest -v tests/test_worker_sanitize_sandbox.py tests/test_herdr_worker.py tests/test_worker_baseline_anchor.py`
- 语法与静态校验：`/opt/homebrew/opt/python@3.13/bin/python3.13 -m compileall -q herdr services bin tests` 与 `git diff --check`
- S6 代码审查报告：`.omc/review-e4c9f87a-2df8-4f84-96dd-5ab219879f6f.md` (MERGE_READY)

---

## 124. 启动身份与替换义务必须穿过真实生产接缝（2026-10-02）

### 问题背景

wf-project-1002-01 的 plan Worker 在创建 Pane 后报 FileNotFoundError：早期 .herdr-launch-identity.json 被真实 git clean -fd 删除，随后身份更新失败。临时 Git + Worker.main 新建分支/onto 回归均失败；旧测试 mock 了 Worker 返回值，没有执行这个接缝。同一工作流中，完成标记冒号后空格漏识别，文档本地分支误作为远端 onto，作废任务缺替代者却允许节点放行。

后续 test 派发暴露另一处接缝：integrated 只发布实现 task ref，源码 HEAD 仍旧。Controller 因 HEAD 不等冻结 SHA 省略 pin；CLI 又仅在 onto 存在时向 Worker 转交 pin。直接借用实现任务分支会被合法所有权保护拒绝。

### 经验教训

内部文件分类只影响产物核算，不能保护文件免受 Git 清理。新状态字段只加在 CLI 会漏掉 Kernel/Store/CAS。测试直接 save_task 构造 superseded 会跳过原子转换新增的义务字段，也可能掩盖合法候选复用被错误阻断。配置校验后重新读取再冻结会引入检查与使用不同输入的窗口。

### 操作规范

仅保留本次启动身份，清理失败在 Pane 前停止，Pane 后失败保留恢复依据。替换义务在既有 SQLite 转换事务统一生成；缺替代者拒绝，显式放弃与配置必需项分开，当前候选 verifier reuse 继续按真实证据满足。Run 冻结同一次已校验读取，不继承已知外来任务绑定。marker 接受格式必须与 prompt 净化同形，软换行后仍检验完整 ID。

无 onto 的验证派发也要完整传递当前冻结 SHA，并让 Worker 从已证实存在的不可变 commit 创建自己的分支，保留实现分支所有权。用真实 CLI→Worker→Git→SQLite→读取链验证 candidate、baseline、HEAD 一致；Worker 返回值替身不能证明基线。模型 headers timeout 后若同一会话已经成功完成，不中断、不重复投递，不把自然恢复说成已部署的 Provider 修复。

### 验证命令 / 关联证据

`pytest -q tests/test_workflow_stall_regressions.py tests/test_reverification_controller.py tests/test_state_transition_gateway.py`；真实 Git、临时 SQLite、CLI→Kernel→Store→Controller 读取与外部 UI 传输替换分别验证。红绿证据 .omc/stall-red-*.log、stall-green-*.log；最终全量和生产边界见 docs/walkthroughs/20261002-wf1002-stall-fixes.md。不把本地绿色称为生产恢复。

---

## 125. 工作流跨 Run 快照隔离、控制器 CPU 空转风暴消除与 Worker Push 治理（2026-10-03）

### 问题背景

真实业务工作流（如 `wf-project-1002-01`）在执行过程中出现全节点停滞：
1. **跨工作流状态污染**：上一个工作流的 `required_task_ids` 残留在项目共享 `workflow.json` 中，新工作流启动时原样继承，导致调度器 `node_is_complete` 永远在寻找上一个工作流的任务，实现节点永远判为未完成，下游测试与评审节点永远得不到触发就绪信号。
2. **控制器 91.8% CPU 空转风暴**：`services/herdr-controller.py` 轮询 363 个任务时，对所有处于 `cleaned/committed/integrated` 的 264 个任务每秒反复调用 `workflow_closed`，每次新建 SQLite 连接并执行 4 条 PRAGMA，触发全库 29 张表与 30 多个复杂触发器的模式解析（AST 编译），造成控制面严重拥塞。
3. **Agent 自行 Push 触发 Git Adoption 死锁**：实现任务提示词要求 Agent 提 PR，Agent 在沙盒克隆内执行 `git push origin`，导致 Git Adoption 判定为 `foreign_commit_in_range` / `current_branch_mismatch` 并进入 `commit_refused` 死锁。
4. **Jev 语义判定 HTTP 422 报错**：`herdr/observer/signals.py` 向 Jev API 传入字符串类型 `criteria`，违背 TypeSafe SystemOne Schema 契约导致 422 校验拒绝。

### 经验教训

| 问题 | 教训 | 规范 |
|---|---|---|
| 项目级定义与单次 Run 状态生命周期混淆 | 跨 Run 复用可变文件会造成动态运行态字段（如 `required_task_ids`、`active_task_ids`）污染新工作流 | 启动新工作流必须进行深拷贝快照隔离，并强制清理顶层与节点级的动态运行时字段 |
| 轮询主循环高频跨库查询引发模式解析风暴 | 终态判断不全导致循环中高频对非活动任务重复执行 SQLite 连接与触发器编译 | 扩展终端状态集合（`TERMINAL_LIKE_STATUSES`），引入轮询批次内的 `_wf_closed_cache` 局部缓存，彻底消除无效连接风暴；同时确保 `committed` 任务的终化重试不被饥饿丢弃 |
| Worker Agent 沙盒越权 Push 制造安全冲突 | 仅凭提示词纪律无法约束自主 Agent 向远端执行 `git push` | 在沙盒装配阶段物理安装双重 Push Guard（`pre-push` 拦截脚本 + `remote.origin.pushUrl=DISABLED_FOR_WORKER_LOCAL_TEST_ONLY`），彻底阻断沙盒越权推送；代码交付由平台统一收编 |
| 第三方语义模型 Schema 契约不匹配 | 题型入参未严格遵循官方 Schema 导致请求被校验中间件拦截 | `noul` 题型引导词合并到 `instructions`，移除不合法的字符串 `criteria`；HTTP 异常处理必须完整回显服务端返回的 Body |

### 操作规范

1. **`herdr/projects.py`**：在 `register_workflow` 中强制调用 `_snapshot_workflow_definition`，并通过 `_clean_workflow_definition_for_new_run` 彻底清洗 `required_task_ids`、`task_ids`、`active_task_ids`、`status`、`error` 等动态字段。
2. **`services/herdr-controller.py`**：定义 `TERMINAL_LIKE_STATUSES = ("completed", "failed", "superseded", "cleaned", "committed", "integrated", "cleanup_ready")`，利用局部闭包缓存减少 `workflow_closed` 查询，并将终化重试条件精确覆盖到 `status in ("completed", "committed")`。
3. **`services/herdr-worker.py`**：引入 `install_worker_sandbox_push_guard`，兼容 `core.hooksPath` 并重定向 `remote.origin.pushUrl`。
4. **`herdr/observer/signals.py` & `herdr/decision/providers/jev.py`**：将引导提示并入 `instructions`，移除字符串 `criteria`，并在 `_http_post_json` 中暴露完整的 HTTP 422 错误载荷。

### 验证命令 / 关联证据

- 专项测试套件：`pytest -v tests/test_commit_adopt.py tests/test_git_adoption.py tests/test_workflow_snapshot_isolation.py tests/test_jev_criteria_schema.py tests/test_worker_push_guard.py tests/test_review_blockers_regression.py tests/test_finalize_git_index_wait.py tests/test_herdr_worker.py` (92 passed)
- 全量自动化测试：`pytest -q` (3180 passed, 157 subtests passed)
- 代码静态检查：`python3 -m compileall -q herdr services bin tests` 与 `git diff --check`

## 126. 配置拒绝必须贯穿读取与呈现；自动验收不得代替评审（2026-10-03）

### 问题背景
FIX_BUG1002 handoff 的 required ID 指向外部 workflow，导致任务已集成但节点永远 pending。新增入口校验后，独立审查又复现 `_safe_workflow` 吞掉配置异常，丢失 required IDs，使卡片反而显示 completed。另有已有 blocked 或同节点对抗评审任务被自动完成、验收长调用期间状态变化的边界。

### 经验教训
fail-closed 不能只存在于解析核心；装配、错误转换和呈现也必须保留拒绝语义。受控变更证据不等同于评审通过。观察模式 PAUSE 只是一条建议，应从日志和事件的 enforcement 字段区分实际干预。

### 操作规范
配置载入异常保留明确诊断，卡片不得落入缺省完成规则。跨 workflow 仅用于诊断归属，不借用其任务成果。自动验收通过现有 CAS 绑定状态与版本，证据写入原状态事件；保持 pass/blocked 裁决契约。Pane已分配后的失败先区分never-started与start-requested。已证明本进程私有动态分配且未尝试start可rollback；跨进程回收还需split回执terminal_id及实时token核验。未知实例/曾启动执行保留，失败clone安全归档而非删除。managed分配/回收共享全局文件锁；原生close无CAS且人工直接native操作不受锁约束，需明确竞态边界。

### 验证命令 / 关联证据
`pytest -q tests/test_fix_bug1002.py tests/test_auto_acceptance.py tests/test_herdr_task_ops_center.py tests/test_worker_readiness_contract.py tests/test_supervisor_interception.py`；`tests/test_fix_bug1002.py` 覆盖临时SQLite CAS、配置异常→卡片、真实CLI note写入→公开读回及Worker故障注入。全量结果见 `.omc/verify-FIX_BUG1002.md`；本地验证不等于生产验收。


### FIX_BUG1002 全范围续修补证

恢复能力必须有真实操作入口，不能只写“身份未知需人工处理”。`launch-reconcile`提供带完整参数的恢复命令，旧token绑定要求显式never-started证据与reason；认证只写tag/audit，后续apply才回收。legacy完成恢复按run/version签发receipt-v1，声明、验收、合并、部署仍分开，不由idle或文件推定成功。状态继续复用rework，禁止working机械退pending。

交付顺序修复集中在平台：task先集成、同候选独立review/test pass，再按明确SHA发布PR。H-2保持，不能为允许任务自行push而降低归属门禁。fetch/push远端仓库身份需一致；未知POST结果通过远端open PR inventory恢复，避免重复创建。配置调整发布本workflow不可变快照，expected-sha阻断陈旧修改，指针与审计同事务，不改共享项目配置。

同节点多角色隔离要检查历史任务和reservation，不仅检查正在working的任务；显式Agent也经过同一锁与审计路径。预检必须验证真实已注册adapter请求能力，局部刷新不抬全局时间；Controller/Console解释DAG使用节点集合，current_stage只在唯一前沿时派生。

外部NexusArchive `gitee-pr.sh --head`需同时校验当前源分支，不能仅作为push目标；校验在认证/API之前完成，再按解析的SHA推送。独立CoW修复与HAFlow平台交付是两个验收对象，不等于原工作树已更新或外部PR已发布。

证据：`tests/test_fix_bug1002_lifecycle.py`、`tests/test_fix_bug1002_routing.py`（含独立进程role竞争）、`tests/test_fix_bug1002_config.py`（真实CLI与独立并发修改）、`tests/test_fix_bug1002_delivery.py`（真实本地HTTP provider及Git远端）。外部Nexus `scripts/test/test-gitee-pr-head.sh`新5 passed/0 fail/0 skip、既有160 passed/0 fail/0 skip，日志`/tmp/FIX_BUG1002-nexus-gitee.log`；尚未应用业务原工作树。当前源码全量结果待主控最终填写，不复用旧版统计；尚未部署、重启或真实外部发PR。

#### 最新handoff六项补证

冻结候选身份与任务分支身份分开：并行test/review各自创建任务分支，实际HEAD精确等于candidate SHA；共享onto不能通过放宽ownership修复。本地专属onto没有SHA应在持久intent之前拒绝。

completed是接受状态，不是集成证据。物理收尾检查真实Git staged/dirty/untracked产出，转写后及关闭/删除动作前重新检查，发现新产出保留现场并失败返回；workflow外层不能忽略retained子结果继续标记完成。受控交错真实Git回归曾证明只检查开头会删除转写期间新产物。

PR发布必须核验review/test的verified_candidate_sha，声明candidate_sha与pass不足以证明现场候选。默认dispatch_role=worker是传输角色，不能遮蔽真实test/review节点；node优先stage以免旧stage伪装implementation。

Nexus CoW integration分支复用Agent白名单、branch/worktree全部context归属，不能一概接受herdr前缀或提前绕开protected分支。外部脚本回归192项通过；HAFlow最终全量结果随后补证。

最终补证：26项本地处理完整，当前代码全量3244 passed、2 skipped、157subtests，0failed；跳过为隔离HOME无launchd agent目录的安装测试。范围/版本/命令/证据和未执行项见 `docs/product-specs/fix-bug1002.md`。

### PR144 合并前审查补证：副作用回执和不可变配置（2026-10-03）

**现象与根因**：Worker在native split返回后、锁外写launch tag前崩溃，磁盘仍workspace_created；另ensure_node_runtime把动态tab/anchor写回node-config-set的内容寻址snapshot，历史config_sha失效。真实子进程退出与update_required_tasks→ensure_node_runtime故障注入分别复现。

**修复**：split后在managed lifecycle锁内持久回执；持久写失败仍从本次已分配内存身份恢复并保留目录。运行拓扑存入StateStore node_runtime及同事务audit，配置读取只overlay拓扑，node-config CAS读取原始不可变字节。

**验证**：tests/test_fix_bug1002_lifecycle.py 子进程os._exit和receipt OSError；tests/test_fix_bug1002_config.py真实配置更新→runtime装配→snapshot hash/event SHA→后续CAS。原生split与磁盘写间不具跨系统原子性，未持久未知结果仍须显式身份恢复。

**操作规范**：状态提交、回执落盘与native副作用是不同边界，覆盖成功和写回异常路径；内容寻址文件禁止承载易变运行拓扑。继续使用既有StateStore，不创建平行权威源。

### CoW目录与Git元数据隔离须分别核验（FIX_BUG1002收尾）

**现象与根因**：Nexus沙盒文件目录已复制，但.git为文本，`git rev-parse --git-common-dir`仍指向外部worktree公共Git；通用finish因此枚举外部gemini工位，并因dev被主worktree占用停止。文件系统CoW不自动隔离Git指针。

**处置**：在任何worktree删除前停止，复制公共Git到本沙盒私有.git，隔离私有副本的继承worktree登记，再核对absolute-git-dir指向沙盒；不删除外部worktree、文件或其当前引用分支。squash落地用API merged及is_branch_landed内容等价共同证明，不把通用forge探测失败当作未合入。

**证据**：PR1518 merged，dev `e5db586ad`；收尾记录和原指针已归档到本任务_archive。原始git-dir指向`nexusarchive/.git/worktrees/gemini`，修复后指向FIX_BUG1002-nexus/.git。

**操作规范**：复制工作区后编码前同时检查.git类型、absolute-git-dir、git-common-dir和worktree list；跨目录指针必须先独立化。通用收尾不得清理继承的外部工位登记所指目录。


## 127. 多智能体流水线系统性硬化：补派闭环、原子启动、环境信任与门禁静态校验（2026-10-04）

### 问题背景
在多智能体流水线长周期运行中，暴露了四类阻断节点推进和引发挂起的系统级隐患：
1. 补派断链：重试或替代任务发起时未指定 `--supersedes`，导致 Scheduler 持续报错 `zombie_obligation_unreplaced`，后续节点永远无法推进；
2. 启动意图超前：Worker 在原生 Agent 进入 `interactive_ready` 之前就过早推进并持久化了 `agent_started` 意图，就绪阻塞或失败时留下孤儿动态 Pane 和僵尸分配意图；
3. 环境信任副作用外溢与挂起：Worker 运行时尝试暗中改写全局配置引发并发冲突或安全告警；未受信任的工作区在后台启动时弹出 TUI 交互弹窗导致静默挂起；
4. 门禁重言式缺陷：人工或模型手写门禁判定脚本时未核对 TSV Schema，出现列越界（如 `$5 > 4`）或同列自比（`$2 == $2` / `$4 != $4`）甚至互斥枚举自比等重言式恒真或恒假漏洞，破坏流水线安全把关。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 替代与重试未关联旧任务 | 失去血缘追踪（lineage）会遗留未清偿义务，导致调度器僵死 | 凡是替代已作废任务的启动，必须强制指定 `--supersedes <prev_task_id>` |
| 过早持久化就绪意图 | 外部进程失败可能发生在意图声明与实际可用之间 | 状态持久化必须位于就绪确认之后；未就绪失败幂等回收动态分配，意图置 `resources_absent` |
| 启动回滚误删工作区 | 销毁 Clone 工作区导致无法排查启动失败真实原因（如 TUI 报错或进程日志） | 失败回滚仅关闭分配的动态 Pane，严禁删除已尝试启动的 Clone 现场 |
| 运行时动态写全局配置 | Worker 作为独立工位进程严禁越权污染宿主全局配置 | 工作区信任（Grok / Claude）前置沉淀在 Factory 初始化装配期预埋 |
| 手写门禁判定脚本逻辑缺陷 | 门禁 Shell / awk 脚本存在重言式恒真或恒假隐患 | 引入独立语法与 TSV Schema 静态语义校验器，在 DAG 编译期 fail-closed |

### 操作规范
1. **补派契约**：在 `bin/herdr-task launch` 和 `direct_dispatch` 中固化校验：若当前节点存在未清偿义务（`status == 'superseded' and replacement_pending and not superseded_by`），强制要求 `--supersedes`；
2. **启动原子化**：Worker 仅在 `startup_readiness` 确认 `interactive_ready: True` 后持久化 `agent_started`；未就绪异常下，若为动态 Pane 则受锁关闭，向父进程报告 `disposition='rolled_back', recovery_required=False`，主进程执行 `abort_launch_intent`；
3. **保留排查证据**：受 `agent_start_attempted` 保护，尝试启动后的 Clone 目录绝不删除；
4. **环境装配分离**：工作区信任通过 `herdr/workspace_trust.py` 在 `herdr-factory` 初始化装配期预埋，Worker 运行时只读；
5. **门禁静态语义校验**：`herdr/gate_validator.py` 在工作流装载时校验 Shell 引号平衡、awk 大括号配对、列越界及同列比较重言式。

### 验证命令 / 关联证据
- 新增单元测试：`pytest tests/test_dispatch_supersede_enforcement.py tests/test_workspace_trust.py tests/test_gate_validator.py tests/test_worker_startup_atomic_rollback.py` (15/15 passed)；
- 核心回归测试：`pytest tests/test_worker_readiness_contract.py tests/test_herdr_worker.py tests/test_dispatch_idempotency.py tests/test_node_capacity.py tests/test_workflow_stall_regressions.py tests/test_preflight_runtime_contract.py tests/test_dynamic_workflow_schema.py tests/test_workflow_engine.py tests/test_workflow_snapshot_isolation.py tests/test_workflow_lifecycle_matrix.py tests/test_fix_bug1002_lifecycle.py` (176/176 passed)；
- S6 独立评审工件：`.omc/review-0f4d0031-5b2c-4b9d-b665-f57c61df269f.md`（MERGE_READY）。

---

## 128. 路由健康拒绝解耦（防节点容量死锁）、签发笔记全链路接线与候选重冻闭环（2026-10-04）

### 问题背景
在多智能体流水线任务派发与审计加固中，暴露了 5 项影响可用性与可解释性的缺陷：
1. **健康拒绝落 failed 锁死槽位**：Router 在 Deep Preflight 探测失败或未就绪时，原逻辑无差别调用 `_record_router_failure_task` 在 `tasks.json` 持久化记录失败任务。在 `max_tasks_per_node=1` 的单工位节点上，瞬态网络或探针抖动导致槽位被永久耗尽，后续自愈重试直接死锁；
2. **CLI 误导性隔离提示**：Agent 健康探测失败时，CLI 输出了针对跨阶段隔离的“Isolation is fail-closed... opt out”提示，误导用户通过 opt-out 绕过，掩盖了真实的环境与凭证问题；
3. **共享文档区缺失 `kind=sign-off`**：`NOTE_KINDS` 未纳入 `sign-off`，导致审批签发记录无法被机器验证通过，且无法在后续节点计算上下文相关度时得到权重赋分；
4. **重复派发日志歧义**：`[DISPATCH DUPLICATE]` 仅打印既有任务 ID，造成提议 ID 与存量 ID 混淆；
5. **缺少 CLI 候选重冻入口**：全仓缺少显式重冻候选 SHA 的命令，难以在复验阶段由运维或上层编排主动触发重冻。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 瞬态健康失败写入持久化任务终态 | 探测失败（健康/连通性）与安全违规（跨阶段隔离）是完全不同的故障语义 | 仅安全底线（隔离阻断）持久化失败以满足 FR-6 审计；健康拒绝仅清理内存/意图资源（`abort_launch_intent`），绝不消耗节点配额 |
| 异常类型非结构化导致误导提示 | 使用通用的 `RuntimeError` 导致上层只能靠粗暴的兜底文案处理错误 | 建立领域异常层级（`RouterIsolationRejection` / `RouterHealthRejection` / `RouterPolicyRejection`），上层精准分流恢复与诊断提示 |
| 审计文档类型定义不完备 | 流程中合法存在的签发（sign-off）行为若不在 Schema 白名单中会被拒绝或当作未知 | 核心文档常量 `NOTE_KINDS` 与 `CONTEXT_KINDS` 必须同步覆盖所有受管生命周期笔记 |
| 日志标识单向模糊 | 遇到去重拦截时，只报已存在者无法定位是谁发起了冲突提议 | 去重日志必须对称输出 `proposed` 与 `existing` 双向 ID |
| 候选冻结仅存在内部 API | 无 CLI 暴露使得人工介入或独立脚本无法执行关键编排动作 | 编排核心动作必须具备幂等 CLI 入口，且严格遵循最新事件幂等的剧集语义 |

### 操作规范
1. **异常体系**：在 `herdr/agent_router.py` 定义继承自 `RuntimeError` 的类型化异常，保证向下兼容；
2. **拒绝处置分流**：在 `bin/herdr-task launch` 中引入 `_is_isolation_rejection`：
   - 隔离拒绝：持久化失败任务，输出 fail-closed 及 opt-out 引导；
   - 健康/策略拒绝：执行 `abort_launch_intent` 并释放 reservation，不再写入 `tasks.json`，输出 deep-preflight 自检引导；
3. **签发笔记接线**：在 `herdr/workflow_docs.py` 中将 `sign-off` 加入 `NOTE_KINDS` 与 `CONTEXT_KINDS`；
4. **派发日志优化**：`bin/herdr-task` 在拦截重复时统一打印 `[DISPATCH DUPLICATE] proposed={args.task_id} existing={duplicate['task_id']}`；
5. **候选重冻命令**：`bin/herdr-task freeze-candidate <workflow_id> --candidate-sha <SHA>` 调用 `record_candidate_frozen`，严格依据最新一条记录判断幂等（支持 A -> B -> A 重新轮转）。

### 验证命令 / 关联证据
- 专项测试套件：`pytest -v tests/test_router_health_and_defects_remedy.py` (7/7 passed)；
- 关联回归套件：`pytest tests/test_agent_router*.py tests/test_workflow_docs*.py tests/test_canary_router.py tests/test_dispatch_idempotency.py tests/test_dispatch_candidate.py tests/test_frozen_base_candidate_dispatch.py` (139/139 passed)；
- S6 独立审查报告：`.omc/review-b80982fa-74b8-4bce-9898-736219f8e996.md`（MERGE_READY）。


## 129. 局部门禁安全不能替代全局恢复活性（2026-10-05）

### 问题背景
`wf-nexusarchive-1005-01` 出现 test cleaned/blocked、implementation committed/finalize_escalated、review 未派发。旧路径只从完成工作流或 READY 节点的依赖发现 fix-loop；AND join 未就绪导致修复入口消失。Console 手工推进正确拒绝 blocked，却没有统一持久执行入口。

### 经验教训
每个入口拒绝非法推进只能保证局部安全，不能保证失败事实始终有负责人、有期限、有后续动作。恢复应由失败事实触发，独立于正向 DAG 与 Agent 是否存活。注册、实际派发、交付、验收必须分别取证，不能用单个“完成”状态替代。

### 操作规范
在原 SQLite 事务中同时写 Task 与恢复义务；语义身份排除活动时间，以 CAS 租约认领，副作用前重查代次、候选、谱系。unknown 交付禁止盲目重发；人工 retry/hold/verify 绑定当前版本。committed 前驱保留历史，通过已确认交付的 successor 修复；不得为修复而先集成失败候选。失败提交可能仅存在前驱 clone，后继必须从包含精确 SHA 的干净登记仓库克隆；项目根与克隆源不能混为一谈。每个原受影响目标必须有完整修复映射；同 Run rework 还必须绑定本轮 request ID 与实际交付回执，不能复用上一请求的 delivered 标记。最终结案必须验证新候选全部门禁。该约束已固化到恢复专项和跨入口回归测试。

### 验证命令 / 关联证据
- `pytest -q tests/test_recovery_entrypoints.py tests/test_recovery_store.py tests/test_recovery_successor.py tests/test_workflow_progress.py tests/test_workflow_recovery.py`
- `tests/test_recovery_entrypoints.py#test_controller_records_failure_even_without_coordinator_or_ready_join`：并行 review 缺席且无 coordinator 仍登记。
- `tests/test_recovery_entrypoints.py#test_failed_candidate_never_finalizes_via_legacy_watcher`：修复前 RED 到达 integrate transport，修复后拒绝集成。
- 最终计数和未验证项记录于本轮 `.omc/verify-1005fixbug.md`；不将旧测试通过作为本轮或线上验收证明。


## 130. 阶段推进锁自愈必须覆盖自身谱系死亡（2026-10-06）

### 问题背景
在 `wf-nexusarchive-1005-01` 真实死锁排查中，`test` 门禁处于阻塞，fix-loop 作废了已派发的任务，但后续替代任务并未落地（`replacement_pending=True` 且 `superseded_by=None`，沦为僵尸义务）。当前节点发生回退，而其前驱依赖 `implementation` 保持 `completed`。旧的 `reconcile_stage_advance_states` 仅在前驱不完成时撤销 `'notified'` 状态锁，导致该节点被判定为前驱完好而一直保留 `'notified'` 锁。Controller 在每轮扫描中 `mark_stage_advance_queued` 均返回 `False`，导致该节点被永久静默跳过，工作流陷入死锁。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 阶段推进锁仅检查前驱是否回退 | 节点自身的任务谱系全部作废且无活跃替代任务时，节点同样已回退 | 推进锁自愈必须双向判定：不仅检查前驱完整性，在前驱完整时还必须检查自身节点名下是否还存在活跃任务 |
| 活跃任务过滤器排除了被作废的任务头 | 判定节点是否需要重派发时，被作废的任务是替代者缺席的直接证据 | 引入 `node_tasks_for_latch`，保留全量任务（含 `superseded`），供重派发候选器分析谱系活性 |

### 操作规范
1. 在 `herdr/direct_dispatch.py` 中提供 `node_tasks_for_latch(tasks, workflow_id, node_id)` 获取节点名下的全量任务；
2. 在 `services/herdr-controller.py` 的 `reconcile_stage_advance_states` 中：在前驱完成的分支下，通过 `lineage_redispatch_candidates` 检查自身谱系是否存在无活跃替代任务的作废头；若存在，以 `"own lineage fully superseded with no live task"` 为由主动释放 `'notified'` 锁；
3. 补充 4 项单测回归用例，防范自愈不触发或误撤销存活任务的锁。

### 验证命令 / 关联证据
- `pytest -v tests/test_stage_advance_and_supersede.py` (29 passed, 9 subtests)
- `pytest -q tests/test_stage_advance_and_supersede.py tests/test_direct_stage_dispatch.py tests/test_dispatch_*.py tests/test_controller_*.py` (185 passed, 35 subtests)


## 131. 运维中心一键修复与门禁放行彻底解耦（防自动越权豁免）、前置归属校验与显式人工审计（2026-10-05）

### 问题背景
在 `allinai0506/HAFlow` 运维中心驾驶舱的异常中枢中，暴露了严重的门禁越权与安全隐患：
1. **自动修复失败隐式放行门禁**：`console/herdr_factory_console.py` 的 `api_controller_execute_action` 在处理 `ops_repair` / `retry` 动作时，对 `rework` 和 `redrive` 异常进行了静默吞错，随后级联调用 `herdr_kernel.force_pass_gate` 与 `manual_advance`。当测试门禁卡点任务无可用恢复动作或恢复失败时，系统擅自将 `stage_verdict` 篡改为 `pass` 并推进下游，击穿了流水线质量门禁；
2. **人工豁免缺乏独立通道与显式约束**：人工强制放行缺少必填原因说明和二次确认，前端在异常卡片点击即可触发默认模版原因的放行请求，且缺少对目标门禁节点的明确范围限制（容易隐式全流放行）；
3. **过期与错配请求未严格拦截**：未对当前请求的 `workflow_id`、`task_id`、节点与运行实例进行前置权威校验，存在已作废（superseded）任务仍被触发恢复或跨工作流错配的风险；
4. **推进失败伪报成功**：当门禁放行成功但后续 `manual_advance` 推进失败时，直接吞错返回成功，未真实反馈部分完成状态。

### 经验教训

| 问题 | 教训 | 规范 |
|------|------|------|
| 修复失败后降级调用更强力的放行操作 | “一个动作报错就试下一个更强的动作”是严重的安全坏味道；普通修复绝不能升级为豁免 | 彻底解耦恢复通道与豁免通道：自动修复仅执行安全工位动作，不适用或失败必须保留阻塞 |
| 人工豁免与普通修复混淆在同一交互逻辑 | 豁免是高风险行为，必须具有独立意图、显式确认与不可抵赖的责任审计 | 强制放行必须显式确认（`confirmed: True`）、强制填写人工原因、明确指定目标门禁节点 |
| 过度依赖前端校验而忽视服务端前置防御 | 前端弹窗或禁用按钮容易被并发或非法请求绕过 | 服务端在执行任何动作前必须重新读取权威存储，强校验工作流归属、节点匹配、版本与运行实例 |
| 级联操作部分失败时整体伪报成功 | 门禁豁免已持久化但工作流推进受阻属于典型 partial 状态，伪报成功会误导排障 | 明确区分全部成功与部分完成，向调用方和前端返回 `partial: True` 及真实推进错误 |
| 会签批准直接调用放行内核绕过防护 | 业务审批入口与底层内核调用未统一接入门禁前置校验，导致审批可以被作为绕过通道 | 会签批准必须接入统一放行校验网关，要求明确确认、非空反馈与快照版本 |
| 多任务节点以最新任务状态代表全节点完成 | 较早失败且未被替换/豁免的任务被最新任务的通过或完成掩盖，产生虚假完成幻象 | 状态聚合中严禁以 `latest_v == 'pass'` 穿透掩盖失败；未豁免的失败任务必须如实保留 failed |
| 节点级放行允许单一版本号代表任务集合 | 单一版本号无法证明任务集合未被并发增删或替换，相同版本数值会引发并发越权覆盖 | 存在有效任务的节点级放行必须提供完整的 `expected_task_versions` 映射严格核验所有任务 |
| 空节点放行未绑定空快照引发并发新增越权放行 | 预检认为节点无任务而跳过快照要求，写入事务前并发插入的任务会在未被确认的情况下被放行 | 节点级放行必须显式绑定任务快照映射（无任务时传 `{}`），并在 SQLite 事务内严格比对实际任务集合，集合不一致立即完整回滚 |
| 较新任务完成/集成掩盖较早任务失败 | 多任务节点若以最新任务处于 completed/integrated 态兜底判定全节点完成，会掩盖未经替换或豁免的早期失败 | 状态聚合中未被替换（未置 `superseded` / `superseded_by`）、未被明确豁免的失败任务，独立阻断节点判定完成，严禁基于时间戳排除 |
| 旧门禁放行记录无失效边界掩盖新阻塞 | 放行记录未绑定任务版本与节点任务集合，导致任务再次阻塞、新增阻塞任务或回溯重跑后旧记录仍继续覆盖 | 豁免记录必须显式绑定生效任务及版本快照；建立统一有效性判定函数（`is_gate_override_valid`），任务重新产生阻塞结论、版本漂移或节点集合变化立即失效；回溯（`rollback_workflow`）级联清除受影响节点 active 豁免并转入历史审计 |

### 操作规范
1. **解耦通道**：彻底删除 `ops_repair` / `retry` 中的 `force_pass_gate` 与 `manual_advance` 兜底降级；仅按当前状态确定并执行 `rework` / `redrive`，无安全动作明确返回拒绝；
2. **异常不吞**：命令失败或超时错误直接向外抛出，保留原始执行信息，严禁继续发送另一条恢复命令或自动放行；
3. **前置强校验**：核验工作流与任务存在性、拒绝 `status == 'superseded'` 任务、核验 `req_node == task_node`、核验版本与 pane 实例，错配一律拒绝；
4. **人工放行契约**：要求 `confirmed=True`、非空且非默认模版的原因说明、明确归属于该工作流的门禁节点；推进失败如实返回 `partial: True` 与 `advance_error`；
5. **前端交互加固**：卡片放行按钮绑定二次确认弹窗，强制输入原因，取消不发请求；引入 `_opsActionBusy` 信号量防止连击重复提交；会签操作舱支持显式确认与非空反馈；
6. **聚合与映射闭环**：多任务节点聚合消除以最新任务 completed/integrated/pass 掩盖未豁免失败任务的逻辑，历史任务必须通过显式替换（`status == 'superseded'` 或 `superseded_by`）排除；节点级放行强制要求 `expected_task_versions: dict`（无任务节点必须显式提供 `{}`），并在 SQLite 强事务内严格校验实际任务集合与提交映射完全一致，出现并发新增或替换时坚决回滚；
7. **放行失效边界与回溯闭环**：`force_pass_gate` 显式写入单任务或节点级任务集合与版本快照，并将历史审批追加至 `gate_overrides_history`；在 `herdr/workflow_graph.py` 沉淀统一有效性判定 `is_gate_override_valid`，阶段摘要与画布投影共用，一旦任务再次阻塞、版本漂移或节点任务集合变化立即失效；`rollback_workflow` 级联作废下游受影响节点的 active `gate_overrides` 并保留审计记录，杜绝回溯重做现场被旧记录穿透覆盖。

### 验证命令 / 关联证据
- 新增专项测试套件：`pytest -v tests/test_console_ops_repair_gate_separation.py`（25/25 passed，涵盖单节点/多任务/并发竞态/会签全流程/节点映射全场景/空节点交错快照拒绝/较新完成任务防失败掩盖/已豁免任务再次阻塞失效/新增阻塞任务失效/回溯自动失效与审计保留）；
- 阶段摘要回归测试：`pytest -v tests/test_console_stage_summary.py`（11 passed, 3 subtests passed）；
- 画布投影回归测试：`pytest -v tests/test_workflow_graph_projection.py`（18 passed, 3 subtests passed）；
- 控制台与工作流回归测试：`pytest -q tests/test_workflow*.py tests/test_console*.py`（453 passed, 79 subtests passed in 41.39s）；
- 全项目 Python 语法与字节码编译：`python3 -m compileall -q herdr services bin tests console`（clean compile）；
- 代码格式检查：`git diff --check`（clean diff）。


---

## §132 跨 PR 测试隔离：新守卫逻辑提前拦截时需精确 mock 而非注释掉断言

### 问题背景
PR #151（ops 修复与门禁解耦）与 PR #152（持久恢复义务）先后合并入 main。将 #152 合并进 #151 分支后，`api_controller_execute_action` 新增了一条优先级最高的守卫：

```python
if any(op['status'] not in {'resolved','superseded'}
       for op in api_workflow_recovery(wid)['operations']):
    raise RuntimeError('该工作流存在持久恢复义务，请通过恢复待办裁决，禁止重复派发或绕过门禁')
```

该守卫在所有业务校验（含"缺少 expected_version"）之前执行。测试夹具写入 blocked 任务时，`save_task` 自动调用 `ensure_for_workflow` 注册恢复义务，导致 PR #151 中专项测试 scenario 6、7、17 及 `test_console_controller_actions_api.py` 中"缺少版本约束"子断言全部失败——错误信息变为恢复义务拦截而非版本校验拦截。

### 经验教训
1. **守卫顺序即测试路径**：生产代码的防御层按顺序执行，任何新的优先守卫都会改变测试命中的分支。合并入依赖 PR 后必须重新审视所有断言"命中哪一层校验"。
2. **精确 mock，不要拓宽或删除断言**：正确修法是对"专门测试某一校验层"的子用例注入 `patch.object(c, "api_workflow_recovery", return_value={"operations": []})` 隔离上游守卫，而不是放宽断言（改为 `pytest.raises(RuntimeError)` 不检查消息）或注释掉子用例。保留下游守卫的真实断言——recovery 义务测试仍使用真实调用。
3. **mock 粒度匹配子用例粒度**：scenario 17 含 3 个独立子断言（缺少版本/陈旧版本/匹配版本），每个子断言独立包裹 mock，避免 mock 状态溢出影响其他子步骤。
4. **合并后立即重跑专项**：两个 PR 功能上互不冲突，但测试路径彼此干扰。合并后最先运行受影响的专项套件，确认失败原因，再决定是修改生产代码还是修复测试隔离。

### 操作规范
- 合并依赖 PR 后，立即运行受影响专项测试套件，不要只跑全量等出结果。
- 新增优先守卫时，同步检查其他 PR/分支的测试是否有子用例依赖"守卫未命中"的路径，在 PR 描述中注明。
- 测试中需要绕过某守卫时，优先 `patch.object` 精确注入，保留被测层的真实断言，禁止仅改 `pytest.raises` 不检查 message。

### 验证命令 / 关联证据
- PR #151 合并提交：`28725d6637df44f945d9639a25ab2eee6ce85d21`（fix(ops): merge main + test isolation）
- 专项套件：`pytest -q tests/test_console_ops_repair_gate_separation.py`（25 passed）
- Controller 动作套件：`pytest -q tests/test_console_controller_actions_api.py`（7 passed）
- 全量回归（排除已知超时 HTTP 集成测试）：`pytest -q --deselect tests/test_console_ops_repair_gate_separation.py::test_scenario_9_http_route_integration_to_sqlite`（3535 passed, 1 deselected, 157 subtests passed in 551.75s）


## §133 重复修复必须验证恢复消费，而非只验证发送（2026-10-06）

### 问题背景
`wf-nexusarchive-1005-01` 的 PR #144–153 修复了局部守卫，但历史身份缺失、已送达动作未消费、任务分支所有权混用和活动时间冻结候选仍组合成死锁。专项绿灯后的独立审查再次复现空恢复 inventory 重发，以及已送达 successor 自身成为 affected 后被再次返工。

### 经验教训
发送前判断与送达后消费是两套必须同时验证的边界。Workflow/Run/epoch/候选/请求身份不能由时间或执行声明推测；恢复后的副作用必须基于当前权威快照。业务测试报告不能由通用测试分数替代。外部证明正确也不能把慢 Git/文件读取放在全局 SQLite 写锁内；迁移审计不应复制原始任意 payload。

### 操作规范
统一事务快照、同代发布 episode、Task 专属引用、交付一对一映射、短写事务 CAS 和白名单逆迁移。返工只消费当前失败，已经在途的正式后继只等待结果。验收列出实际配置的全部 AC-N，并绑定校验后的 checkpoint；显式预算增加保留历史计数。上述反例已固化到 `tests/test_workflow_repair_contracts.py`，不得以放宽断言替代修复。

### 验证命令 / 关联证据
`pytest -q tests/test_workflow_repair_contracts.py tests/test_recovery_store.py tests/test_workflow_recovery.py tests/test_recovery_entrypoints.py` 实际 87 passed。工作树 `.omc/evidence/` 保留第一轮 7 个 RED 反例及第二轮 successor 自体 RED；生产数据库只读 backup 的新迁移 apply/rollback 演练有独立 plan/receipt。以上不代表已部署或 NexusArchive 业务验收成功。

生产复跑补证：test-r6 的 METRICS 明确 business_acceptance=unknown，但旧 EVALUATION 成功模板仍声称 DoD 完全满足、安全完成工单。已追加真实 RED 回归 test_generic_success_report_does_not_certify_business_or_completion；两个通用报告模板均只声明所选命令及通用评分，不认证业务验收或完成授权。

生产前进补证：review最新业务blocked且operation894等待时，Controller仍靠stage pass派发wrapup（26838/26846）。业务证明须在所有前进入口授权，而非只在恢复结案时读取；共享guard覆盖Controller/CLI/kernel/PR/close，拒绝发生在intent/push/teardown前，未知不造返工义务。另SQL review预算6但launch读旧workflow.json的4，已用固定config优先与损坏旧文件回归封堵双权威。

#### 2026-10-06: verifier cohort freshness
- Problem: review proof could turn blocked while a later test artifact was being hashed, after review had passed an individual freshness check.
- Cause: per-gate CAS does not establish a consistent multi-gate authorization snapshot.
- Resolution: validate all external artifacts first, then compare every receipt, task version, candidate episode, pinned config and active cohort together in one read-only SQL transaction.
- Prevention: real kernel and PR regressions inject a newer blocked review, config changes and a new verifier head during later artifact validation.

#### 2026-10-06: filter before bounded teardown inventory
- Problem: 1800 internal test evidence files exhausted the raw Git inventory cap before infrastructure paths could be filtered.
- Cause: untracked-file expansion preceded the existing diagnosis filter.
- Resolution: summarize untracked directories in Git before applying the unchanged output cap; preserve individual tracked-change detection.
- Prevention: real Git regressions distinguish large internal-only evidence, untracked user directories and tracked internal changes. Treat OS probe failures as unknown and reject teardown. Preserve historical documents with Run-bound checkpoints and reversible artifact relocation without rewriting verdicts or bypassing branch hooks.

### 首派回执与后续恢复的生命周期冲突（#156）

**现象**：首个Task故障作废后，扫描识别需要补派，但resolved首派记录不允许claim，队列长期为空。

**根因**：首次登记事实被复用为整个节点后续派发的准入状态，历史完成记录与当前恢复责任混在一起。

**修复**：保留旧resolved，为合法替代谱系建立独立派发义务，身份绑定前序Task/Run；扫描和协调器读取同一当前记录，替代登记验证supersedes。

**防复发**：用真实Controller/CLI/SQLite覆盖首派登记、故障自动作废、四轮扫描、替代登记及重复派发幂等；同时验证部分替代、再次故障和旧identity拒绝。

### 控制契约接管旧状态与运行模式（#156第二次复审）

**现象**：新责任记录缺少存量历史时丢弃合法补派；另一起点有任务使空起点未建责任；关闭intake后旧pending阻断后继；非-rN替代Task已登记仍生成多余补派。

**根因**：新契约将历史记录当准入前提、按Workflow总任务判断节点，缺少未发送责任的模式移交，并将名称当作唯一谱系事实。

**修复**：按节点接管存量补派；退役未发送责任并移交direct scheduler，保留已发送未知核验；追加epoch而不重置旧事实；显式supersedes合并既有命名谱系。

**防复发**：真实Controller/CLI/SQLite验证存量恢复、多起点、pending与已预占待办切换模式、重新启用、已发送未知不重发，以及投递结果未知但非-rN替代Task已登记。
## §134 修复循环的 verdict 指纹不得含易失实例标识，重置预算必须保留语义指纹（2026-10-06）

### 问题背景
现场实测：test-01-r3 终结后，test-01-r4 作为后继代任务重记录同一 blocked 结论，`handle_fix_loop` 判为新 verdict 再次作废并扣 loop 预算，反复打事件。

### 经验教训
`verdict_fingerprint` 把 blocker task_id 混入摘要，而 task_id 是逐轮更换的易失实例标识——同一语义 verdict 在每轮重试后必然"变新"，`is_repeat_verdict` 永远失配。同时闩释放（redo 完成）会连指纹一起清除，预算重置后同一结论可以无限次重新进入循环。去重/预算类机制的键必须由语义内容（branch、结论说明、affected 目标）构成，任何随轮次变化的实例字段都会让"重复"不可判定。

### 操作规范
1. verdict 指纹只含语义维度：suggested_branch + blocker note + affected_task_ids（目标敏感性由 affected 承担，PR #110）。
2. 闩释放清计数（新 redo 新预算）但保留 `|fp`：后继代重记录同一结论走 repeat_verdict 升级，不得用新预算再开一轮。
3. 预算扣减必须以"发生了新的语义 verdict"为前提，而不是"存在可作废对象"。

### 验证命令 / 关联证据
`pytest -q tests/test_fix_loop_recovery.py tests/test_selective_replan_core.py tests/test_fix_loop_gates.py tests/test_selective_replan_controller.py` 180 passed；修复前三个新回归（指纹跨代一致 / 后继同 verdict 升级不扣预算 / 闩释放保留指纹）均 RED 复现缺陷，修复后转绿；全量 `pytest -q` 3593 passed + 157 subtests。变更：`herdr/fix_loop.py` verdict_fingerprint 去 task_id；`services/herdr-controller.py` `_fix_loop_latch_blocks` 释放路径保留 `|fp`。

## §135 指标口径必须覆盖全部 runner；不得用一端计数证明另一端增量（2026-10-06）

### 问题背景
test-01-r5 的 acceptance2 曾拿前端 vitest 计数（4248）证明后端测试增量，而后端 JUnit 参数化实例（矩阵 98、后端 6905）在受管指标里完全不可见，形成错误判据。根因：`parse_test_output` first-match-wins 且不支持 Maven Surefire/JUnit 格式。

### 经验教训
受管指标的口径缺陷会静默放大为验收误判。解析器逐格式匹配后立即返回，意味着"第一个认得的 runner"定义了全局口径；跨技术栈组合日志（前端 vitest + 后端 JUnit）必须聚合，且聚合必须防同格式双计（surefire 的 per-class "Time elapsed" 行与 Results 汇总行并存）。

### 操作规范
1. 新增测试 runner 接入评估器时必须同时更新口径测试（tests/test_evaluator_multi_runner_metrics.py）。
2. 验收判据引用受管计数时，先确认计数覆盖了被判据约束的测试面；覆盖不到即声明 unknown，不得以另一端计数替代。
3. gitignored 的内部装配文件不得用 git 排除式 pathspec（`:!path`）排除——会撞 ignored-file advice 使 add 必然 exit 1；先 add -A 再 `git reset -- <paths>` 摘除。

### 验证命令 / 关联证据
`pytest -q tests/test_evaluator_multi_runner_metrics.py tests/test_autosave_clone_wip.py tests/test_workflow_closed_cli_error.py tests/test_pane_transcript_archive.py` 全绿（前两类修复前三用例 RED 复现现场症状）；全量 3604 passed + 157 subtests，唯一失败 test_no_spacing_grid_violations 经 pristine main 复跑确认为 #157 引入的存量失败。

## §136 采样事件绑定当前版本号时，旧屏幕残留会自证新鲜；必须以持久在场周期鉴定内容的新旧（2026-10-07）

### 问题背景
wf-project-0929 审计复现 C03c：任务被 BLOCKER 观察置为 blocked 后，显式恢复（blocked→working，版本号递增）时 Pane 屏幕上的旧 BLOCKER 标记不消失；Sentinel 巡检把同一屏幕内容绑定任务**当前**版本号记成新的 `blocked_marker_observed`；Controller 的版本 CAS 无法识别它来自恢复前旧屏幕，任务再次 blocked。"拒绝旧事件"与"恢复仲裁队列"等既有修复都覆盖不了"旧屏幕被重新采样成新事件"这条路径。

### 经验教训
屏幕字节无法自证新旧，而"绑定采样时刻的任务版本"会让任何残留内容在下一次巡检自动获得新鲜版本号——版本匹配只能证明"样本在状态写入之后采集"，证明不了"内容产生于状态写入之后"。完成路径的 `observe_completion` 早已用持久行 + epoch 重置 + absent→present 周期解决同类问题（旧屏幕重放），但同构的 BLOCKER 路径没有移植该纪律，于是同一类缺陷换个入口复发。鉴别内容新旧的根本手段只有内容侧的时间序列证据：真实新事件之前必然存在一次"内容不可见"的观察，残留则永远连续在场。

### 操作规范
1. 观察类事件若绑定任务当前版本号，必须同时在持久层维护所观察内容的在场状态（如 completion_observations.blocker_present），事件与在场状态同事务落盘；采样判定以"先前样本是否已被消费 + 在场周期/版本事实"分层：未消费样本在版本失效时必须可重采样（否则元数据写即造成漏报）；版本仍有效时，无关屏幕刷新不得重复采样，否则 Controller 不可用期间每轮 record 会增长事件并持续跳过 pending steer。
2. 已消费（阻塞已发生并恢复）之后，屏幕字节差异不能作为"新发生"的证据——恢复动作本身（如恢复指令注入 pane）就会改变字节，凭差异重采样会造成"恢复动作触发再阻塞"的循环；该象限只由"标记消失→再现"的完整巡检周期重新武装。极端的亚巡检间隔再耗尽是接受性漏检，由人工与停滞观察兜底，须在文档中如实声明。
3. 内容读取失败（空捕获/rc!=0）不得当作"不可见"记录；决策逻辑收敛为核心纯函数（blocker_sample_action），外壳只做装配；同一观察对象的新增持久状态复用既有行/表结构（只增列、状态不变不写），不得另建平行事实源。
4. 在场事实与业务动作的状态门槛分开：巡检已在 blocked/rework 读到的 absent 也必须保存；新阻塞样本仍只在 dispatched/working 产生。否则合法返工期间已经观察到标记消失，恢复后仍会被旧在场记录永久抑制。

### 验证命令 / 关联证据
合并前补充验证：同版本变屏去重、真实 steering 投递、rework/blocked 期间 absent 持久化共 5 条回归先失败，最小修复后 `pytest -q tests/test_blocker_resample_discipline.py` 26 passed。原两项 P1 另经真实 Controller CAS、SQLite 写后失败回滚及独立连接并发探针验证。基点 `cd6b95e` 与 PR `87106b2` 的真实 main() 对照确认 rework 漏记 absent 为本 PR 引入。此前四轮评审与 22 例记录属于旧版本；合并主干后的全量结果和最终独立评审以 PR #161 最新验证记录为准。变更：`herdr/state_db.py` observe_blocker_marker/_blocker_sample_consumed、`herdr/completion.py` blocker_sample_action、`services/herdr-sentinel.py` BLOCKER 分支与 pane_visible、`herdr/state_store.py`。

## §137 人工恢复必须重建责任并绑定所确认的身份（2026-10-07）

### 问题背景
取消补派与历史派发未登记是两种卡点。原通用“重试”无法改变取消范围，主图仍显示无阻塞；未完成启动记录又使安全重试长期不可用。

### 经验教训
按钮存在不代表恢复路径成立。将未知交付直接重置 pending 会复活旧命令；只校验 operation 版本不能发现表单打开后 Task/Run 已变化。终态 registered intent 也不能一律视为未结案，必须用同 Task/Run 的真实登记与终态验证。

### 操作规范
人工决策绑定候选、代次、operation version 和精确 Task/Run/version；缺 Run 不猜测，缺 execution 要求显式确认并审计。旧责任永久退休，事务创建新 epoch；资源核查复用既有 inventory，归属未知仍阻止重发。启动前与登记写事务都阻断迟到旧调用。外部核查只读采集，最终事务复验版本与精确 intent，再一起提交资源回执、待办结果与审计；不能静默跳过版本冲突却返回成功。UI 成功区分责任建立和真实任务登记，读取失败不得宣称无卡点。

### 验证命令 / 关联证据
`tests/test_dispatch_recovery_ui.py` 覆盖真实 Console API→SQLite→Controller、独立连接竞争、注册后取消、旧调用拒绝、身份变化、缺失上游身份与迟到表单结果；浏览器临时 Console 表单另行验证。部署或业务结论须使用本次交付记录，不能由单元测试推断。

## 132. 文件范围必须容纳工程交付，提交拒绝不能只做盲重试（2026-10-07）

### 问题背景
wf-nexusarchive-1007-01的实际派发仅允许三Java文件，bugfix分支commit门禁同时要求复盘。临时仓库执行原hook复现退出1。Controller重复提交耗尽，错误事件只保留前500字，关键拒绝原因被Node检查挤掉。

### 经验教训
任务目标、文件授权和仓库交付要求必须同时成立。提示词提醒不等于机器检查，完成声明不等于提交成功；直接提交收编和重启回执也必须覆盖相同边界。错误输出先脱敏再取有界尾部，JSON与带空格引号值必须测试。扩展恢复身份时不能让旧责任哈希变化。

### 操作规范
在节点明确登记产物、范围及安全验证命令，范围冲突先裁决。完成消费和commit都校验新鲜证据，收编路径不能旁路。失败写事务校验Task/Run/version与实际回执；发送未知不重发，已有成功回执只补状态。未提交交付返工与冻结候选测试分开，原工位有限补齐后仍经过实际hook和集成。

### 验证命令 / 关联证据
`pytest -q tests/test_task_delivery.py tests/test_delivery_rework.py`：覆盖范围冲突、真实hook拒绝、收编拒绝、旧Run并发结果、JSON脱敏、发送后中断、旧恢复身份及实际临时Git集成链。生产升级与原工作流复跑另需实际证据。

## 133. 含 fix 任务必须在 plan 期机器拦截缺复盘派发，提示词纪律不能代替门禁（2026-10-08）

### 问题背景
任务名 `impl-atomic-fix` 使分支以 `-fix` 结尾，命中目标仓 bugfix 门禁正则（要求缺陷复盘文档），commit 连败后转人工。复盘发现派发契约只允许了业务文件、未登记复盘产物，而 `COORDINATOR_DISCIPLINE` 第 4 条的“派发前登记”只是提示词纪律，没有机器检查；§132 修了提交侧分流与盲重试，但 plan 期仍可放行缺复盘的 fix 派发。

### 经验教训
命名信号必须转成机器门禁：task_id/task_type/node/分支按词边界命中 fix/bugfix/hotfix 的 git 任务，若已登记契约却无复盘类 `required_files`，在 plan 期直接拒绝。首版门禁过宽（连无契约旧链路一起拦，全量 6 failed）证明拦截面必须与兼容语义对齐：无契约走原兼容路径，只拦已登记契约但缺复盘条目者——这正是当年事故的精确形状（契约有三 Java 文件、缺复盘）。

### 操作规范
纯函数放 `herdr/task_delivery.py`（词边界启发式 + 通用复盘路径模式 `bug|report|postmortem|retrospective|复盘|review` + `validate_fix_retrospective`），`herdr/workflow.py` normalize 与 `bin/herdr-task` launch 预检双点调用，均在副作用前抛可操作错误（含示例路径）。不在 HAFlow 硬编码目标仓正则；不更名分支、不加 `--no-verify`。

### 验证命令 / 关联证据
`pytest -q tests/test_task_delivery.py` 新增 `test_fix_task_requires_retrospective_contract`（RED→GREEN：正反例、normalize 拒/放、launch 级纯校验、无契约兼容）；全量 `pytest -q` 3878 passed；独立评审 MERGE_READY（`.omc/review-kadian-xiufu.md`）。

## 138. 派发前必须预埋克隆路径信任，Worker 启动失败必须回写路由健康（2026-10-08）

### 问题背景
auto 路由两次选中 grok，均卡 `TRUST_REQUIRED` 后靠人工改显式 codex 脱困。根因两层：Deep Preflight 只探 `project_root` 的信任，而 grok 按精确路径匹配信任文件，CoW 克隆子目录从未被预埋；Worker 启动失败只打印退出，不回写 `unhealthy_agents`，auto 重试确定性再次选中同一 agent。

### 经验教训
探测环境与运行环境不一致时，探测通过不等于运行通过：凡运行时路径与探测路径不同的资源（信任、凭证、挂载），必须在派发侧对真实运行路径重做预埋。同时失败必须回写健康快照，否则重试是确定性复现而非恢复——`TRUST_REQUIRED` 本就在硬故障集合，进不了快照就永远起不了作用。

### 操作规范
launch 选定 agent 后、Worker 启动前对本次 clone 路径做该 agent 信任预埋（复用 `ensure_workspace_trust`，warn-only 不阻拦派发；Worker 运行时只读纪律不受影响，预埋发生在 launch 外壳）。Worker 输出命中 `TRUST_REQUIRED` 时合并回写 `unhealthy_agents` 并同步投影，auto 重试自动换候选。

### 验证命令 / 关联证据
`pytest -q tests/test_workspace_trust.py`（预埋写入、失败识别、健康合并正反例）；全量 3884 passed，唯一失败为干净主干同败的 #170 遗留；独立评审 MERGE_READY（`.omc/review-kadian-trust.md`）。

## 139. 快路径 defer 必须说出原因码，verdict 空转不靠猜（2026-10-08）

### 问题背景
DB 已 `agent_done` 但 verdict 不触发，协调循环表现为空转，只能人工 sign-off＋advance。快路径条件刻意严格（门禁要文件＋屏幕双信号一致），人工是设计内兜底；缺的是可见性——defer 时没有任何记录指明是哪一个条件不满足。

### 经验教训
凡是有多条件短路的快路径，miss 时必须输出机器可读的原因码，否则排障只能重读全部源码。原因分类必须是纯函数，与快路径条件同义且同测，避免诊断与实现各自演化。

### 操作规范
`classify_verdict_deferral` 纯分类（非门禁：开关/blocked verdict/review 角色/无变更；门禁：开关/信号缺失或冲突），`verdict_defer_reason` 薄封装复用既有谓词，done 事件 miss 且仍 `agent_done` 时打一行 `[VERDICT DEFERRED]`。不动快路径条件、不自动推进。

### 验证命令 / 关联证据
`pytest -q tests/test_auto_acceptance.py`（VerdictDeferralUnitTest 正反全覆盖）；全量 3890 passed（唯一失败为 #170 干净主干同败）；独立评审 MERGE_READY（`.omc/review-kadian-verdict.md`）。

## 140. 信任预埋不得按集成模式设门槛，任何新 clone 启动都需要信任（2026-10-08）

### 问题背景
wf-nexusarchive-1008-01 plan-challenger（grok，integration=none）`Worker startup TRUST_REQUIRED` 回滚：#172 的预埋只覆盖 git 模式，但 Worker 无论何种集成模式都会在全新 clone 目录启动 agent。回写端正常（grok 标 unhealthy 后 executor 用 codex 恢复），基于实时日志 20 分钟内定位。

### 经验教训
资源需求看的是运行时行为，不是任务分类：信任、凭证这类“进程在新目录启动即需要”的东西，门槛只能设在“有无新目录＋agent 有无信任门”上。按 integration_mode 设门是把分类维度错接到资源维度上。

### 操作规范
`preseed_trust_target(clone_path, agent)` 纯决策（模式无关），launch 期 warn-only 调用。handler 映射收敛为模块级 `_TRUST_HANDLERS` 单一事实源。

### 验证命令 / 关联证据
`pytest -q tests/test_workspace_trust.py`（模式无关用例 RED→GREEN）；全量 3891 passed（唯一失败为 #170 干净主干同败）；独立评审 MERGE_READY（`.omc/review-kadian-trust-all.md`）。线上：预埋后新 clone 应出现在 `~/.grok/trusted_folders.toml`。

## 141. 恢复动作必须以持久化终态为准，不以 CLI 返回码为准（2026-10-08）

### 问题背景
wf-nexusarchive-1008-01 test/review-auto 隔离失败后，恢复路径与并发重派同时 supersede 同一任务，`supersede --allow-pending` 报 `Illegal transition: superseded -> superseded` ERROR——尽管替代链已建立、requeue 已推进，恢复仍被记为失败。

### 经验教训
任何“先读快照、再调 CLI”的恢复动作都存在 TOCTOU 窗口：失败返回码只说明本次调用没生效，不说明目标没达成。恢复判定必须重读持久化状态——已达终态即成功，只有仍停滞才算真失败。

### 操作规范
`recover_router_isolation_tasks` supersede 非零后重读任务：已 superseded（状态或 `superseded_by` 任一）即判恢复；仍 failed 才记 ERROR。调用方 fire-and-forget，返回值变化无外部影响。

### 验证命令 / 关联证据
`pytest -q tests/test_impl_fix3_regression.py`（并发 supersede 正例＋真失败负例）；全量 3892 passed（唯一失败为 #170 干净主干同败）；独立评审 MERGE_READY（`.omc/review-kadian-router-recovery.md`）。

## 142. 自动收尾失败必须退避重试，inflight 闩只防并发不防重复（2026-10-08）

### 问题背景
wf-nexusarchive-1008-01 逻辑完成后，close 因 requirements-executor2 未落盘 abort，`maybe_close_completed_workflow` 每 sweep 重调 close——18 连击 abort 风暴＋DB 事件复写。`_workflow_close_inflight` 只在线程内存活期间防重入，失败摘闩后下轮照发。

### 经验教训
任何“失败即摘闩”的派发都必须配失败退避，否则 abort 会变成 sweep 频率的日志/DDoS。复用既有 attention 基建（note＋throttle＋blocks check），指数退避 60s 起 600s 封顶；成功清闩。transport 异常（超时等）与非零返回必须同语义记退避——超时恰是最可能复发的失败。

### 操作规范
只加派发节流，不改 close 判定与终态闸门；人工落盘后下个窗口即收敛。不设 give-up 上限（重试本身有价值）。

### 验证命令 / 关联证据
`pytest -q tests/test_liveness_guard.py`（退避边界/窗口跳过/失败记 episode/transport 异常/成功清闩）；全量 3896 passed（唯一失败为 #170 干净主干同败）；独立评审 round1 NEEDS_FIXES（传输异常漏记）→修复→round2 MERGE_READY（`.omc/review-kadian-close-backoff.md`）。

## 143. 总指挥停滞直派降级与失败计数的生命周期闭环（2026-10-08）

### 问题背景
PR #160 契约核查发现：Controller 原有降级机制仅在总指挥长等待超时（600s）时累计 `attempts`；而遇到总指挥 Pane 缺失（`not coord_pane`）或 Agent 进程立即崩溃（prompt exit code != 0 或执行异常）时仅清空阶段闩，未调用 `attention_note` 记录失败计数，导致反复崩溃无法累加 `attempts`，永不触发直派降级阈值（`attempts >= 2`），工作流永久卡死。此外，若失败计数未随成功推进清理，偶发崩溃会导致历史残存计数与未来的独立偶发故障跨时段拼接，误触发直派降级。

### 经验教训
1. **快速失败分支不能逃逸降级统计**：超时往往是慢崩溃，进程崩溃与资源缺失是快崩溃。错误处理路径若仅做清理（`clear_stage_advance`）而绕过 Attention 事实沉淀，会造成"越严重的立即故障越无法触发自愈"的反直觉死锁。
2. **失败计数必须有明确的成功清零点**：任何基于阈值的熔断/降级机制，其失败计数必须与业务成功闭环绑定。缺少清零机制会导致状态单调递增，将跨越数小时的多次无关联偶发故障误判为持续性雪崩。
3. **顺序双写不能伪装成原子事务**：状态机闩文件与 AttentionStore 分属不同物理存储，顺序写入存在非原子窗口。必须通过显式异常日志暴露清理失败，并配合时效窗口（`task_stall_after` 30 分钟）作为兜底，杜绝静默残留。

### 操作规范
1. **快失败与慢超时统一事实落盘**：总指挥 Pane 缺失、Agent 执行异常、返回码非 0 与长等待超时，统一接入 `attention_note`，以严格契约 `reason="coordinator_stalled"` 累积 `attempts` 并记录 `last_attempt_at=now` 与详细错误上下文。
2. **推进成功即刻清理 Attention Episode**：在 `mark_stage_advance_notified` 入口将推进成功与 `attention_clear` 绑定，对齐目标节点标识 `target_node_id`，以 `try...except` 捕获异常输出明确告警日志。
3. **时效截断与非连续故障重置**：计算递增前检查 `(now - last_attempt_at) > task_stall_after()`（30 分钟）；超时则自动重置为 1（判定为两次独立偶发故障而非连续故障）；接单入口处增加 `is_recent` 时效性约束，拒绝基于过期记录回退。

### 验证命令 / 关联证据
`pytest -v tests/test_coordinator_auto_heal.py` 18 passed，包含：连续 Pane 缺失/Agent 崩溃/执行异常累积降级、单次故障不误降级、成功推进后彻底清零、过期故障重置计数（> 30 分钟）以及清理异常安全日志捕获；全链路套件 `pytest -q tests/test_coordinator_auto_heal.py tests/test_direct_stage_dispatch.py` 65 passed；`compileall` 与 `git diff --check` clean。

## 144. 事实核验三态裁决与反事实证据的作用域守卫（2026-10-08）

### 问题背景
在 Reviewer 引入自动化 Finding Verifier 的初步实践中，裁决逻辑曾采用强二元状态（verified / rejected），且主要依靠简单静态正则搜索反事实证据（如在前 100 行搜索 import、文件内搜索属性赋值）。然而，代码引用存在不等于缺陷成立，匹配局部语句也不等于在缺陷路径上发生；当面对同名变量、条件赋值、不可达分支或局部导入时，静态反证极易误杀真实的检出（TP）。

### 经验教训
1. **语义对称收紧：无充分证明不判 verified，无充分反证不判 rejected**：证明缺陷成立需要完整触发路径与后果证据，反驳断言亦需要确凿的全路径否定。对于无法静态证明或反驳的断言，降级保留为 `uncertain` 是唯一安全的做法，绝不可强行二元化裁决。
2. **反事实反驳必须有作用域与版本守卫**：反证必须与原断言处于同一代码版本、同一函数/类作用域及相同执行前置条件。脱离作用域的“全局存在”不能作为驳回局部缺陷断言的依据。
3. **主门禁保持稳态，验证器旁路加固**：在事实核验器尚未建立完整符号执行与数据流分析前，保持 `rule` 为阻断主门禁、LLM 为影子模式，事实核验器仅对证据确凿的断言做清洗，宁可保留存疑项也不可错误拦截真缺陷。

### 操作规范
1. **三态判定模型**：输出严格区分 `verified`（充分证据证明成立）、`rejected`（充分反证推翻断言）、`uncertain`（证据不足保留人工/上层仲裁）。
2. **反事实证据守卫**：反事实反驳规则需检查行号边界、AST 节点作用域与赋值可达性，无法排除条件覆盖时一律标记 `uncertain`。
3. **对抗性测试全覆盖**：对同名局部变量、条件分支赋值、局部延迟导入、异常分支赋值等对抗性场景建立回归测试，确保真实 TP 零误杀。

### 验证命令 / 关联证据
`pytest -q tests/test_finding_verifier.py tests/test_review_benchmark.py` 38 passed；在真实历史 PR 样本回放中，反事实误报拦截率 100%（5/5 纯反事实幻觉被剔除），真实缺陷误杀率 0%（2/2 TP 稳定保留）。

## 145. 硬预算谓词必须用统一退役判据，退役行占槽=派发死锁（2026-10-08）

### 问题背景
wf-nexusarchive-1008-01 中 max_tasks_per_node=2 把 superseded 旧任务也算进硬预算，独立 challenger 工位永远派不出。首修只认 status=='superseded'，round1 评审抓出 recovery 谱系退役行（committed + superseded_by，`link_committed_successor` 只回填指针不改状态）仍占槽——原死锁换一类行复现。

### 经验教训
预算谓词必须复用仓库统一的退役判据（status=='superseded' 或带 superseded_by），单看 status 会漏掉指针式退役。诊断计数（registered/superseded/retired/budget）与拒绝消息要同 PR 对齐；文档四处（schema/cli-reference/wiki/历史 spec 变更行）同步，避免模板作者按旧契约估配额。

### 操作规范
`budget_tasks` 排除 retired；`locked_overflow` 独立于 `overflow`（后者保留原语义）；在役 failed/completed 仍计数；替换在旧任务退役前仍占槽。

### 验证命令 / 关联证据
`pytest -q tests/test_node_capacity.py`（24 passed：退役行正反、真实报错串、满额语义）；全量 3900 passed（唯一失败为 #170 干净主干同败）；独立评审 round1 NEEDS_FIXES（B1 谓词/B2 文档）→修复→round2 MERGE_READY（`.omc/review-kadian-node-capacity.md`）。

## 146. 显式替代可复用当前 pending operation，拒绝必须枚举精确原因（2026-10-08）

### 问题背景
wf-nexusarchive-1008-01 手工替代重派时复用 dispatch-operation-id 被拒（dispatch operation does not authorize），消息不指明哪一条件失败，被迫不带 operation-id 派发丢失账本。DB 实证：目标节点存在 superseded op 与 pending op（started=0、无 deadline_at）。

### 经验教训
准入拒绝必须枚举精确失败条件并附当前 operation id，恢复者不该靠猜。显式替代（--supersedes 指向同节点 superseded 任务）可复用当前 pending operation 保持账本；但豁免必须限定非终态 op（resolved/superseded 不豁免），deadline 只对已启动 operation 生效——pending 无 deadline 字段，误判即误伤。

### 操作规范
纯函数 `can_launch_with_replacement`（终态拦截＋同节点 superseded 指针）；`_launch_refusals` 枚举原因（kind/workflow/node/execution/active/generation/latest/predecessors/ready/prior_delivery/deadline/legacy/started/status）；`validate_task_registration` 同步传递 supersedes intent。

### 验证命令 / 关联证据
`pytest -q tests/test_dispatch_recovery_ui.py`（49 passed：替代复用＋终态拒绝＋精准原因）；dispatch 族 5 文件 176 passed；全量 3901 passed（唯一失败为 #170 干净主干同败）；独立评审 round1 NEEDS_FIXES（终态 op 未拦截）→修复→round2 MERGE_READY（`.omc/review-kadian-dispatch-replace.md`）。
