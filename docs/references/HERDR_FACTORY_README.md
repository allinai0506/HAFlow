# HAFlow (设计档案 / Design Reference)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> 本地多 Agent 编排与自动研发系统设计参考文档  
> 当前定位：**本地 Multi-Agent Workflow Runtime / AI 协同流控制层**

---

## 1. 项目背景

这个项目不是从“我要做一个工作流引擎”开始的。

它来自一个长期存在的问题：

> 已经有越来越多优秀的 AI Coding Agent，例如 OpenCode、Codex、Claude Code、Qoder、Agy、Pi，但它们通常是彼此独立的工具。  
> 真正困难的不是“再多开几个 Agent”，而是：**如何让多个 Agent 在真实项目中长期协作、可调度、可恢复、可审计地完成工作。**

我们尝试过很多工具和方案，也做过多轮工程实验。

最终我们选择 Herdr 作为底层运行空间，因为 Herdr 提供了一个非常重要的基础：

- Workspace
- Tab
- Pane
- Agent
- Terminal
- Agent lifecycle

在这个基础上，我们逐步构建了自己的 Factory 层：

- Project Registry
- Workflow Registry
- Task Registry
- Controller
- Agent Router
- Load Balancing
- Agent Reservation
- Persistent Pane
- CoW Clone
- Baseline Fingerprint
- Workflow Candidate Branch
- macOS Notifier
- Sentinel
- Deep Preflight
- Local Dashboard / Console

这个项目的目标不是“做一个更复杂的 Herdr”。

而是：

> **让 Herdr 成为一个真实可用的 Multi-Agent Runtime。**

---

# 2. 当前系统模型

目前采用的核心映射：

```text
Workspace = 项目 / 工作空间
Tab       = Workflow Node / 阶段
Pane      = AI 工位 / Task 现场
Agent     = AI 执行者
Task      = 工作单
Coordinator = 项目级总指挥
Controller  = 后台调度器
```

当前已经跑通的研发型工作流：

```text
需求分析
  ↓
计划
  ↓
实现
  ↓
测试
  ↓
评审
  ↓
收尾
```

但这个六阶段模型只是当前第一个模板。

后续方向已经明确：

> **Tab 不应该被永久定义成固定研发阶段，而应该升级为通用 Workflow Node。**

也就是说未来可以支持：

```text
招标文件解析
→ 评分项提取
→ 历史项目检索
→ 投标策略
→ 标书生成
→ 合规检查
```

或者：

```text
客户投诉
→ 原因分析
→ 话术生成
→ 人工审核
→ TTS
→ 发送
```

最终目标是：

# Multi-Agent Workflow Platform

---

# 3. 当前真实运行目录

所有自研运行脚本已经统一迁移到：

```text
/Users/user/HAFlow/
```

这是当前正式 Runtime 目录。

主要文件：

```text
/Users/user/HAFlow/
├── herdr-controller.py
├── herdr-notifier.py
├── herdr-sentinel.py
├── herdr-task.py
├── herdr-worker.py
├── herdr_agent_router.py
├── herdr_pane_pool.py
├── herdr_projects.py
├── herdr_preflight.py
├── herdr_deep_preflight.py
└── ...
```

统一 CLI 入口仍然是：

```text
/Users/user/HAFlow-factory
```

---

# 4. Git 基线

`/Users/user/HAFlow` 已经正式 Git 化。

当前稳定基线：

```text
tag: herdr-factory-v1.0-stable
commit: a7fcb4b
```

之后已经继续提交：

```text
0563ffa  feat: add agent preflight checks
711886a  feat: add deep agent preflight
82e920c  fix: resolve agent binaries from login shell
b28e7b9  fix: simplify qoder and agy deep probes
2897182  feat: filter routing by workflow preflight health
```

以后任何改动必须遵循：

```text
修改
→ 本地验证
→ commit
→ 必要时 tag
```

不要再依赖大量散落的：

```text
*.backup
*.fixed
*.old
```

---

# 5. 当前注册项目

## 5.1 nexusarchive

```text
project_root:
/Users/user/nexusarchive

project_id:
nexusarchive-54433229

current Factory Space:
wA

historical Space:
w6

base_branch:
dev
```

当前 wA 的标准节点：

```text
1总指挥
2需求分析
3计划
4实现
5测试
6评审
7收尾
```

其中 requirements / plan 曾因历史误删重新创建：

```text
requirements
tab_id = wA:t8
anchor_pane_id = wA:pH

plan
tab_id = wA:t9
anchor_pane_id = wA:pJ
```

---

## 5.2 xiyu-bid-poc

```text
project_root:
/Users/user/xiyu/xiyu-bid-poc

project_id:
xiyu-bid-poc-a380753e

current Factory Space:
w9

base_branch:
main

coordinator:
w9:p1
```

当前真实阶段结构：

```text
1总指挥
2需求分析
3计划
4实现
5测试
6评审
7收尾
```

requirements Anchor 当前为：

```text
tab_id = w9:t2
anchor_pane_id = w9:pN
```

原来的 `w9:p2` 已不存在。

---

# 6. Herdr Spaces 与 Project 的关系

一个重要结论：

> **1 Project 可以对应多个 Herdr Space。**

例如：

```text
nexusarchive
├── wA  当前 Factory Space
└── w6  历史 Space
```

因此：

```text
Workspace != Project
```

正确模型是：

```text
Project
└── Spaces
    ├── Current Factory Space
    ├── Historical Space
    └── Unregistered Space
```

Herdr Workspace List 是 Space 的事实来源。

`projects.json` 只是 Factory 的注册关系。

---

# 7. Factory 数据目录

核心状态位于：

```text
~/.herdr-controller/
```

主要文件：

```text
~/.herdr-controller/
├── projects.json
├── workflows.json
├── tasks.json
├── stage-policies.json
├── agent-pools.json
├── agent-reservations.json
├── pane-slots.json
├── projects/
│   ├── nexusarchive-54433229/
│   │   └── workflow.json
│   └── xiyu-bid-poc-a380753e/
│       └── workflow.json
├── clones/
└── logs/
```

---

# 8. 后台服务

当前通过 macOS `launchd` 常驻：

```text
com.user.herdr-controller
com.user.herdr-notifier
com.user.herdr-sentinel
com.user.herdr-factory-console
```

运行脚本已统一指向：

```text
/Users/user/HAFlow/
```

验证：

```bash
launchctl print gui/$(id -u)/com.user.herdr-controller
launchctl print gui/$(id -u)/com.user.herdr-notifier
launchctl print gui/$(id -u)/com.user.herdr-sentinel
```

---

# 9. 日常入口

在任意已注册项目目录：

```bash
cd /path/to/project

~/HAFlow-factory run "自然语言需求"
```

例如：

```bash
cd /Users/user/xiyu/xiyu-bid-poc

~/HAFlow-factory run "分析当前评分标准解析问题并修复最优先的问题"
```

查看 Workflow：

```bash
~/HAFlow-factory status <workflow-id>
```

查看项目：

```bash
~/HAFlow-factory projects
```

查看 Agent Pool：

```bash
~/HAFlow-factory agents
```

查看 Persistent Pane：

```bash
~/HAFlow-factory slots
```

---

# 10. Agent Pool

当前支持的内部 Agent ID：

```text
opencode
codex
qodercli
claude
agy
pi
```

注意：

```text
qodercli
```

是 Factory 内部标识。

实际本地二进制是：

```text
qodercn
```

当前映射：

```text
opencode → opencode
codex    → codex
qodercli → qodercn
claude   → claude
agy      → agy
pi       → pi
```

---

# 11. Agent Router

Router 不是简单按顺序挑第一个 Agent。

当前已经具备：

```text
allowed_agents
disabled_agents
stage policy
task_type
current load
reservation load
workflow override
task override
workflow healthy_agents
```

路由原则：

```text
候选 Agent
  ↓
allowed_agents
  ↓
排除 disabled_agents
  ↓
排除当前 Workflow Deep Preflight 不健康 Agent
  ↓
按 stage / task_type 排序
  ↓
计算 active load
  ↓
计算 reservation load
  ↓
选择负载最低 Agent
```

---

# 12. Atomic Reservation

并发任务如果同时读取：

```text
OpenCode load = 0
Codex load = 0
```

会出现两个 Task 同时选 OpenCode 的竞态。

为此已经实现：

```text
agent-reservations.json
+
file lock
```

流程：

```text
Task A
→ Router
→ reserve OpenCode

Task B
→ Router
→ 已看到 OpenCode reservation
→ 选择其他 Agent
```

Task 注册后释放 reservation。

这是多 Agent 真正负载均衡的关键。

---

# 13. Agent Preflight

## 13.1 Shallow Preflight

文件：

```text
~/HAFlow/bin/herdr-preflight
```

检查：

```text
CLI 是否存在
CLI 版本
认证文件提示
disabled_agents
```

运行：

```bash
cd /Users/user/xiyu/xiyu-bid-poc
~/HAFlow/bin/herdr-preflight
```

可以人为禁用 Agent：

```bash
~/HAFlow/bin/herdr-preflight --disable pi
```

当前 xiyu-bid-poc：

```text
pi = DISABLED
```

因为已知 token 不可用。

---

## 13.2 Deep Preflight

文件：

```text
~/HAFlow/bin/herdr-deep-preflight
```

Deep Preflight 会做真实最小模型调用。

当前已验证：

```text
OpenCode  READY
Codex     READY
Qoder     READY
Claude    READY
Agy       READY
Pi        DISABLED
```

真实 adapter：

```text
OpenCode
→ opencode run

Codex
→ codex exec

Qoder
→ qodercn --print --no-session-persistence

Claude
→ claude --print

Agy
→ agy --print
```

执行：

```bash
~/HAFlow/bin/herdr-deep-preflight --deep
```

可能状态：

```text
READY
DISABLED
MISSING
TOKEN_EXHAUSTED
AUTH_REQUIRED
TRUST_REQUIRED
UPDATE_BLOCKED
TIMEOUT
ERROR
UNKNOWN
```

---

# 14. Workflow 启动前健康检查

当前已经加入：

```text
Workflow start
→ Deep Preflight
→ healthy_agents
→ unhealthy_agents
→ 写入 workflows.json
→ Router 只从 healthy_agents 选择
```

示例：

```text
PREFLIGHT_READY=opencode,codex,qodercli,claude,agy
PREFLIGHT_EXCLUDED=pi:DISABLED
```

Workflow Registry 会记录：

```json
{
  "healthy_agents": [
    "opencode",
    "codex",
    "qodercli",
    "claude",
    "agy"
  ],
  "unhealthy_agents": {
    "pi": "DISABLED"
  },
  "preflight_checked_at": "..."
}
```

注意：

> Deep Preflight 的临时健康状态只作用于当前 Workflow。

而：

```text
disabled_agents
```

是项目级长期配置。

---

# 15. Persistent Pane

Pane 不再被视为一次性资源。

当前原则：

```text
Workspace  长期存在
Tab        长期存在
Pane       默认长期保留
Clone      默认长期保留
```

只有显式：

```bash
~/HAFlow/bin/herdr-task purge <task-id>
```

才做物理清理。

目的：

```text
保留 AI 工作现场
保留历史上下文
支持审计
支持回看
支持后续恢复
```

---

# 16. Stage Anchor

每个 Workflow Node / Stage 记下 `anchor_pane_id`，它是下一次 `pane split` 的父窗格。

例如：

```text
requirements
→ tab w9:t2
→ parent w9:pN
```

这个编号的职责：

```text
作为动态 Pane split 的父 Pane
第一个任务直接占用页签自带的窗格，不再另留空的 Anchor
```

一个重要教训：

> `tab_id` / `anchor_pane_id` 是运行时映射，不应该被当成永久事实。

我们已经真实遇到：

```text
workflow.json:
requirements.anchor = w9:p2

Herdr:
w9:p2 已不存在
```

导致：

```text
pane_not_found
```

当前这个问题还是 **未彻底自动化解决的 P0**。

---

# 17. Anchor Self-Heal（待实现，P0）

下一步必须实现：

```text
每次 Task launch 前

ensure_stage_anchor()
```

逻辑：

```text
读取 workflow definition/runtime
↓
检查 tab_id
↓
Tab 不存在？
  → 自动创建 Tab
  → 获取新 tab_id / root pane
  → 更新 runtime
↓
检查 anchor_pane_id
↓
Anchor 不存在？
  → 在对应 Tab 创建新 Anchor
  → cwd = project_root
  → 更新 runtime
↓
继续 Task launch
```

必须遵守：

```text
不得删除历史 Task Pane
不得把历史 Task Pane 当 Anchor
不得因为 Anchor 缺失而重新 provision 整个 Workspace
```

---

# 18. CoW Clone

每个 Task 使用完整项目 Clone：

```text
~/.herdr-controller/clones/<task-id>
```

macOS 使用 APFS CoW：

```bash
cp -cR
```

每个 Task：

```text
独立 Clone
独立 Branch
独立 Pane
独立 Agent
```

这保证多 Agent 并行时不会互相污染工作目录。

---

# 19. Baseline Fingerprint

不能单纯用：

```bash
git status
```

判断 Agent 修改了什么。

因为 Clone 会继承任务开始前已有的：

```text
tracked change
untracked files
```

所以 Task 创建时记录 baseline fingerprint。

验收：

```bash
~/HAFlow/bin/herdr-task verify-baseline <task-id>
```

结果：

```text
BASELINE_MATCH
TASK_CHANGED
```

这是判断 Task 改动的事实来源。

---

# 20. Git Integration

真实项目不能让 Agent 直接污染 main/dev。

当前推荐模型：

```text
Main
  ↓
Task Branch
  ↓
Integration Branch
  ↓
Workflow Candidate
  ↓
Test
  ↓
Review
  ↓
Wrapup
  ↓
最终 Merge / PR
```

---

# 21. Workflow Candidate Branch

这是当前非常关键的架构。

多个 Implementation Task：

```text
impl-01
impl-02
impl-03
```

不能直接进入 main。

它们应该：

```text
Integration Branches
       ↓
Workflow Candidate
       ↓
Test / Review
```

真实使用过：

```text
herdr/workflow-<workflow-id>
```

Candidate 自动化目前仍需要继续完善。

---

# 22. Controller

Controller 是后台状态机。

主要职责：

```text
监听 Agent lifecycle
Task 状态推进
Coordinator 验收
commit / integrate / cleanup
Stage Advance
```

Task 状态包括：

```text
pending
dispatched
working
blocked
agent_done
completed
rework
committed
integrated
cleanup_ready
cleaned
failed
```

---

# 23. Sentinel

文件：

```text
~/HAFlow/services/herdr-sentinel.py
```

用途：

```text
Agent lifecycle 事件漏报
Prompt 卡住
DONE marker fallback
Crash detection
```

Sentinel 是异常兜底层。

不是主业务调度器。

---

# 24. macOS Notifier

文件：

```text
~/HAFlow/services/herdr-notifier.py
```

用于系统级通知：

```text
BLOCKED
FAILED
NEEDS ACTION
WORKFLOW COMPLETE
```

目标：

> 用户不需要长期盯着 Herdr。

正常情况下系统自行工作。

真正需要人工时再通知用户。

---

# 25. Console / Dashboard

本地 Console：

```text
http://127.0.0.1:8765
```

App：

```text
~/Applications/Herdr Factory Console.app
```

当前已经具备：

```text
Herdr Spaces
Workflow 六阶段看板
Task
Agent
Pane
Agent Pool
Persistent Pane
Deep Preflight
指定 Agent
Candidate
阶段推进
日志
告警
```

Herdr Space 分类：

```text
Current Factory
Historical Space
Unregistered
```

Console 的定位：

> **Factory 控制面**

Herdr 本身的定位：

> **Agent 实际工作现场**

---

# 26. 当前 Console 模型

例如：

```text
nexusarchive
├── wA  Current Factory
└── w6  Historical Space

xiyu-bid-poc
└── w9  Current Factory
```

历史 Space 默认只读，不参与当前调度。

后续可考虑：

```text
[查看历史]
[打开 Herdr]
[设为当前 Factory]
```

但这不是当前 P0。

---

# 27. 已经踩过的重要坑

## 27.1 所有 Task 都选 OpenCode

原因：

```text
Router 只是顺序选择第一个候选
```

解决：

```text
load balancing
+
reservation
```

---

## 27.2 并发竞态

两个 Task 同时看到：

```text
OpenCode load=0
```

解决：

```text
Atomic Reservation
```

---

## 27.3 Codex 更新提示阻塞

出现：

```text
Update available
```

解决：

```text
~/.codex/config.toml

check_for_update_on_startup = false
```

---

## 27.4 Claude Workspace Trust

Clone 每次路径不同。

出现：

```text
Is this a project you created or one you trust?
```

Worker 已增加 trust preflight。

---

## 27.5 Agent Name Collision

失败 session 残留后：

```text
agent_name_taken
```

解决：

```text
task-id + pane hash
```

作为 Agent name。

---

## 27.6 `import re` 缺失

`unique_agent_name()` 新增后调用 `re.sub`，但文件漏了：

```python
import re
```

导致所有 Agent launch 崩溃。

这是一个典型教训：

> 基础设施修改必须有 import / syntax / runtime smoke test。

---

## 27.7 错删 Anchor Pane

曾经清理 orphan Pane 时，关闭了某个 Stage 最后一个 Pane，导致整个 Tab 一起消失。

从此明确：

```text
绝不自动删除 Tab
绝不关闭一个页签里的最后一个窗格
同页已经有任务窗格时，可以关掉停在项目根目录且没有 Agent 的空壳
```

---

## 27.8 主仓库不绝对 clean

真实项目可能存在合法 untracked 文件。

因此 integrate 不能要求：

```text
git status == completely empty
```

应该区分：

```text
tracked local changes
baseline untracked
unexpected untracked
```

---

## 27.9 Complexity Gate 项目耦合

最初默认所有项目都有：

```text
scripts/complexity-gate.cjs
```

这是 NexusArchive 特有能力。

现在原则：

> 项目特有 Gate 不能成为 Factory 基础设施硬依赖。

不存在时：

```text
complexity_baseline=disabled
```

---

## 27.10 Controller 全局 Recovery 错误

曾尝试：

```text
启动时扫描所有 completed Task
```

结果旧历史 Task 全部被重新处理，Controller 被拖死。

正确方向：

```text
active workflow
+
finalize_pending
```

精准恢复。

---

# 28. 当前最重要的系统原则

## 原则 1：控制面与执行面分离

```text
Controller / Router
负责调度

Agent
负责执行
```

---

## 原则 2：Agent 是可替换资源

流程不能绑定单一 Agent。

必须支持：

```text
Pool
Router
Override
Disable
Health Check
Fallback
```

---

## 原则 3：Task 必须隔离

每个 Task：

```text
Clone
Branch
Pane
Agent
```

---

## 原则 4：Runtime ID 不是业务事实

```text
tab_id
pane_id
```

只是 runtime mapping。

业务事实应该是：

```text
Workflow Node
Node ID
Node Label
Dependencies
```

---

## 原则 5：系统状态必须可恢复

任何一步都必须考虑：

> Controller 此刻重启怎么办？

---

## 原则 6：历史数据不能污染当前 Workflow

任何恢复 / 告警 /统计都应按：

```text
project_id
workflow_id
```

隔离。

---

## 原则 7：正常自动，异常通知人

目标不是“人完全消失”。

而是：

```text
正常 → 自动
可恢复异常 → 自动修
信息不足 / 高风险 → 通知人
```

---

# 29. 下一阶段架构升级

现在已经明确：

> 当前固定研发阶段必须升级成通用 Workflow Node。

未来模型：

```text
Project
  ↓
Workflow
  ↓
Node
  ↓
Task
  ↓
Agent
```

Herdr：

```text
Workspace = Project / Space
Tab       = Node
Pane      = Task / Agent Workplace
Agent     = Executor
```

---

# 30. Workflow Definition（设计方向）

未来不再写死：

```text
requirements
plan
implementation
test
review
wrapup
```

而是定义：

```yaml
name: software-development

nodes:
  - id: requirements
    label: 需求分析

  - id: plan
    label: 方案设计
    depends_on:
      - requirements

  - id: implementation
    label: 实现
    depends_on:
      - plan
    parallel: true

  - id: test
    label: 测试
    depends_on:
      - implementation

  - id: review
    label: 评审
    depends_on:
      - test

  - id: wrapup
    label: 收尾
    depends_on:
      - review
```

---

# 31. Node 类型（未来）

建议支持：

```text
Agent Node
Human Node
Tool Node
Gate Node
Parallel Node
Merge Node
```

例如：

```text
Agent Node
→ AI 执行任务

Human Node
→ 人工审批

Tool Node
→ pytest / lint / deploy

Gate Node
→ 条件判断

Parallel Node
→ 多 Agent 并行

Merge Node
→ 等待并汇总多个上游
```

---

# 32. Node Agent Policy（未来）

Node 可以定义自己的 Agent 策略：

```yaml
agent_policy:
  preferred:
    - claude
    - codex

  exclude:
    - pi

  min_agents: 1
  max_agents: 2
```

Router 将综合：

```text
Node policy
Deep Preflight
Current load
Reservation
Historical success rate
Cost
Latency
```

最终选 Agent。

---

# 33. 当前 P0

以下问题优先级最高。

## P0-1 Anchor Self-Heal

实现：

```text
ensure_stage_anchor()
```

在 Task launch 前自动修复：

```text
missing Tab
missing Anchor
runtime mapping drift
```

---

## P0-2 Workflow Candidate 自动化

最终应：

```text
最后一个 implementation cleaned
→ 自动生成/更新 Candidate
→ 自动进入 Test
```

而不是人工点按钮。

---

## P0-3 finalize_pending 精准恢复

解决：

```text
completed
但 Controller 在 finalize 前重启
```

要求：

```text
只恢复当前 active Workflow
只恢复 finalize_pending
```

---

## P0-4 Workflow Definition / Node 抽象

将固定 Stage 升级为通用 Node。

当前研发流程变成：

```text
software-development-v1
```

模板。

---

# 34. P1

```text
Console Workflow Designer
Human Node
Tool Node
Gate Node
Parallel Node
Workflow Template
Historical Space 切换
Agent token/quota 监控增强
```

---

# 35. P2

```text
Agent 历史成功率
Agent 平均耗时
Agent 成本
Token 使用
任务类型成功率
动态智能 Router
Workflow DAG 可视化
macOS Notification 点击跳 Pane
```

---

# 36. 当前状态结论

当前系统已经不是 Demo。

我们已经真实跑过：

```text
requirements
plan
implementation
test
review
wrapup
```

并且真实处理过：

```text
Agent 首启
Agent token
Agent update
Agent trust
Git branch
Clone
Candidate
测试失败
返工
Review
Pane 保留
Anchor 丢失
Workflow 恢复
```

这套系统已经证明：

> 多 Agent 可以真实参与软件工程。

但接下来重点必须从：

```text
“继续堆功能”
```

转向：

```text
“抽象、稳定、恢复、模板化”
```

---

# 37. 给 Codex 的工作原则

接下来请把这个仓库当成一个正式工程项目，而不是临时脚本集合。

修改前必须：

```text
1. 阅读 README
2. 阅读相关 Runtime 文件
3. 查看 git status
4. 查看当前 tag / log
5. 不破坏已跑通能力
```

任何修改必须：

```text
先理解现状
→ 最小改动
→ py_compile / smoke test
→ 真机验证
→ git commit
```

禁止：

```text
大范围字符串 patch
无验证覆盖整个运行文件
删除 Tab
删除 Anchor Pane
删除历史 Task Pane
全局扫描所有历史 Task 做恢复
```

---

# 38. 推荐给 Codex 的第一批任务

优先按顺序：

```text
1. Anchor Self-Heal
2. finalize_pending 精准恢复
3. Workflow Candidate 自动化
4. Workflow Definition / Node 抽象
5. Console Workflow Designer
```

---

# 39. 最重要的一句话

这个项目的目标不是：

> “让多个 AI 同时写代码。”

而是：

> **让多个 AI 成为一个可调度、可恢复、可审计、可长期运行的工作组织。**

这才是 Herdr Factory 的核心。
