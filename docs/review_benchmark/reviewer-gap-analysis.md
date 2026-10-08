# HAFlow Reviewer Baseline V2 缺陷与能力缺口分析报告

## 一、评测执行概况

- **评测数据集**: HAFlow Review Dataset V1 (10 个历史真实 PR 案例，10 个 Golden Defects)
- **评测时间**: 2026-10-07
- **基线版本 SHA**: `2bf2a199fb24df3edfefe574839903d44e8b202a`
- **Reviewer Agent**: `rule (baseline.json)` (HAFlow 生产真实评审基准规则)
- **ReviewBench 裁判模型**: `openrouter/anthropic/claude-sonnet-4.6`
- **执行完整性**:
  - `planned`: 10
  - `completed`: 10
  - `failed`: 0
  - `status`: `completed`

---

## 二、评测核心指标对比

| 指标 | Baseline V1 (3 PRs) | Baseline V2 (10 PRs) | 趋势说明 |
|---|---|---|---|
| **Grounded Recall** (基础召回率) | 100.0% | **30.0%** | 数据集扩展后暴露真实覆盖盲区 |
| **Grounded Precision** (基础准确率) | 100.0% | **100.0%** | 保持极高纯度，匹配黄金问题无虚报 |
| **Augmented Recall** (增强召回率) | 100.0% | **35.0%** | 含判定成立的有效新发现 |
| **Augmented Precision** (增强准确率) | 83.3% | **87.5%** | 裁判判定有效性保持高位 |
| **Novel TPs** (成立的新发现) | 0 | **1** (PR #85) | 在 PR #85 检出同模块真实缺陷 |

---

## 三、各缺陷分类检出与盲区矩阵 (Category Breakdown)

| 缺陷分类 | Golden 总数 | 检出 (TP) | 遗漏 (FN) | Grounded Recall | 判定准确率 | 核心能力缺口分析 |
|---|---|---|---|---|---|---|
| **Evidence / Claim** | 1 | 1 | 0 | **100.0%** | 100.0% | 现有规则能有效捕捉派发声明与克隆基线证据脱节问题 (PR #107) |
| **Workflow / State** | 4 | 1 | 3 | **25.0%** | 100.0% | 能捕获代际 task_id 指纹漂移；但遗漏状态逃逸、作废任务误判、异常安全熔断语义 |
| **Identity** | 2 | 1 | 1 | **50.0%** | 100.0% | 能捕获复验 episode 绑定缺失 (PR #108)；遗漏文件命名下划线分隔符碰撞 (PR #100) |
| **Contract** | 2 | 0 | 2 | **0.0%** | — | **完全盲区**：无法识别 SQLite 只读 WAL 模式头检查缺失 (PR #102) 与 DAG 上下文闭包引用限制 (PR #155) |
| **Concurrency** | 1 | 0 | 1 | **0.0%** | — | **完全盲区**：无法通过静态正则分析跨进程/Sentinel 巡检中的 CAS 竞争风暴 (PR #118) |

---

## 四、具体 Case 漏检深因诊断

### 1. Contract 类别完全失守 (0% 召回)
- **PR #102 (`state_db.py:1172-1187`)**: SQLite 只读 WAL 模式下创建 sidecar 文件报错。这是特定系统库 (sqlite3) 与底层 POSIX 文件系统只读挂载交互引发的契约问题。当前规则型 Reviewer 无法做环境行为推理。
- **PR #155 (`state_db.py:5450-5460`)**: 启动上下文校验逻辑未考虑工作流 DAG 依赖闭包阶段。此缺陷属于深层业务语义与数据流闭包契约，浅层模式匹配无法建立 DAG 拓扑感知。

### 2. Concurrency 类别完全失守 (0% 召回)
- **PR #118 (`herdr-controller.py:2860-2885`)**: Sentinel 处理 blocked 观测记录时无前置快照校验，直接发起 CAS 写入造成数据库颠簸。并发锁与 CAS storm 依赖动态吞吐与时序心智模型，单文件 diff 正则无法感知并发竞争。

### 3. Workflow / State 类别大面积漏检 (75% 漏检)
- **PR #103 (`agent_router.py:645-665`)**: 路由决策逃逸出临界区异步写入。涉及事务边界与容灾恢复契约。
- **PR #106 (`rollout_policy.py:387-423`)**: 异常熔断判断未 Fail-Closed。涉及安全策略设计模式。
- **PR #85 (`fix_loop.py:101-126`)**: 作废任务的时间戳误判。虽然 Reviewer 在该 PR 命中了另一个有效缺陷（`fix_loop.py:48-68` 的指纹易失性，获裁判评为 Novel TP），但黄金问题本身被漏检。

---

## 五、对下一代 Reviewer 的演进建议 (不修改当前基线)

1. **引入 AST / 语义控制流分析**: 替代单行/关键词正则匹配，准确识别跨方法、跨代码块的数据与状态流。
2. **引入系统不变量检查器 (Invariant Checker)**: 专门审查 HAFlow 核心架构不变量（如 Fail-Closed 安全熔断、事务临界区收敛、Claim 与 Evidence 强绑定、代际隔离）。
3. **集成模型评审 (LLM-based Reviewer)**: 在保持输入 Blind 隔离的前提下，评估具备深度逻辑推理能力的 LLM Reviewer 在 Contract、Concurrency 维度的审查能力。
