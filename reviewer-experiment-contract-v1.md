# Reviewer Experiment V1 — 提升 Contract 缺陷识别能力 A/B 评测报告

## 实验元数据

- **实验标识 (Experiment ID)**: `reviewer-contract-v1`
- **基线版本 (Baseline)**: `rule` (`haflow-review-baseline-v2`)
- **候选版本 (Candidate)**: `rule-contract-v1`
- **数据集 (Dataset)**: `haflow-review-dataset-v1` (10 Golden PRs, 10 Golden Defects)
- **评测框架 (ReviewBench SHA)**: `e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d`
- **裁判模型 (Judge)**: `openrouter/anthropic/claude-sonnet-4.6`
- **评审 Agent 配置**:
  - Baseline Config Hash: `e740dc862e6e30eb` / Prompt Hash: `eb055dfaeef573ae`
  - Candidate Config Hash: `bb74ed8a212607b7` / Prompt Hash: `d58a59f674cd7855`
- **修改变量 (Changed Variable)**: Prompt (增强通用 Contract 边界与系统不变量推理深度)

---

## 一、核心指标对比矩阵 (A/B 结果)

| 指标 (Metric) | Baseline (`rule`) | Candidate (`rule-contract-v1`) | 差异 (Delta) | 趋势 |
|---|---:|---:|---:|:---:|
| **Grounded Recall** | 30.0% | **40.0%** | **+10.0%** | 📈 提升 |
| **Grounded Precision** | 100.0% | **100.0%** | **+0.0%** | ➖ 持平 |
| **Augmented Recall** | 35.0% | **45.0%** | **+10.0%** | 📈 提升 |
| **Augmented Precision** | 87.5% | **90.0%** | **+2.5%** | 📈 提升 |
| **Contract Recall** | 0.0% (0/2) | **50.0% (1/2)** | **+50.0%** | 🚀 达标突破 |

---

## 二、分类与检出条目细目

### 1. 命中情况对比 (TP / FP Breakdown)

| 类别 | Baseline | Candidate | 变化明细 |
|---|---|---|---|
| **Baseline TP** | 4 (3 Grounded + 1 Novel) | 5 (4 Grounded + 1 Novel) | PR #107 (Grounded), PR #108 (Grounded), PR #110 (Grounded), PR #85 (Novel) |
| **Candidate TP** | - | 5 (4 Grounded + 1 Novel) | **PR #102 新增 Grounded TP** (Contract 类) |
| **Baseline FN** | 7 | 6 | PR #102 从 FN 转化为 TP |
| **Candidate FN** | - | 6 | PR #100, #103, #106, #118, #155, #85(golden) |
| **New TP** | - | **1** | **PR #102** (`herdr/state_db.py:1172-1187`) |
| **Lost TP** | - | **0** | **无已有 TP 丢失** (PR #107, #108, #110, #85 均稳定保留) |
| **New FP** | - | **0** | 无新增误报 |
| **Removed FP** | - | **0** | PR #108 的 1 个跨 PR 调度指纹判定保持 baseline 表现 |

### 2. 条目级明细对比表

| PR 案例 | 目标缺陷文件及行号 | 类别 | Baseline 判定 | Candidate 判定 | 状态 |
|---|---|---|:---:|:---:|:---:|
| **PR #102** | `herdr/state_db.py:1172-1187` | `contract` | ❌ 漏检 (FN) | ✅ **matched_tp** | **+1 New TP (Contract)** |
| **PR #155** | `herdr/state_db.py:5450-5460` | `contract` | ❌ 漏检 (FN) | ❌ 漏检 (FN) | 保持漏检 (Type B 根本缺 Context) |
| **PR #107** | `herdr/scheduler.py:152-165` | `evidence_claim` | ✅ matched_tp | ✅ **matched_tp** | 稳定保留 (No Regression) |
| **PR #108** | `herdr/reverification.py:657-672` | `identity` | ✅ matched_tp | ✅ **matched_tp** | 稳定保留 (No Regression) |
| **PR #108** | `herdr/scheduler.py:152-165` | `correctness` | ⚠️ novel_fp | ⚠️ novel_fp | 稳定保持 |
| **PR #110** | `herdr/fix_loop.py:48-68` | `workflow_state` | ✅ matched_tp | ✅ **matched_tp** | 稳定保留 (No Regression) |
| **PR #85** | `herdr/fix_loop.py:48-68` | `correctness` | ✅ novel_tp | ✅ **novel_tp** | 稳定保留 (No Regression) |

---

## 三、两个 Contract 缺陷深入根因分析 (FN Root Cause Analysis)

### 1. PR #102 (`herdr/state_db.py:1172-1187`) 漏检根因

- **结论**: **Type A（Reasoning 不足 / 推理深度缺陷）**
- **证据与上下文分析**:
  - PR #102 diff 长度仅 10,742 字节，完整进入了 Reviewer Context。
  - Diff 中清晰呈现了核心变更：
    ```python
    +def get_readonly_db_connection(db_path: Optional[Path] = None, *, timeout: float = 5.0) -> sqlite3.Connection:
    +    ...
    +    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=timeout)
    +    conn.execute("PRAGMA query_only = ON")
    +    return conn
    ```
  - **为何 Baseline 漏检**:
    Baseline Reviewer 只检查通用语法与规则，缺乏**系统状态契约与隔离承诺 (Isolation / Zero-side-effect Contract)** 的前置与副作用推导能力。它看到了 `mode=ro` 和 `query_only=ON`，误以为只读模式已完全满足无副作用契约，未能推断出底层的存储引擎契约：*在 WAL 模式下，即便以只读 URI 打开数据库，如果工作目录具备写权限且缺少 sidecar 文件前置校验，底层引擎仍可能意外创建或修改 `-shm` / `-wal` sidecar 文件，从而违背严格的“零副作用诊断隔离”契约。*
  - **本次 Candidate 为何能够检出**:
    通过引入通用 Contract Review Procedure（检查函数对外声明的副作用契约与其依赖的底层运行时契约），Reviewer 能够主动对只读/观察者连接入口进行契约有效性审查，识别出仅凭 `mode=ro` 无法保证零副作用隔离。因此成功报出该缺陷并被官方裁判模型判定为 `matched_tp`。

### 2. PR #155 (`herdr/state_db.py:5450-5460`) 漏检根因

- **结论**: **Type B（Context 缺失 / 关键信息根本未进入 Reviewer Context）**
- **证据与上下文分析**:
  - PR #155 包含 14 个文件的修改，Diff 总量高达 **394,522 字节**。
  - Reviewer 的 Diff 输入存在截断阈值（15,000 字符），`herdr/state_db.py` 的改动位于 Diff 的第 153,169 字节之后，因此 Reviewer 根本无法在常规 Diff 视窗中看到完整变更。
  - 更根本的结构性原因：黄金缺陷定位在 `herdr/state_db.py:5450-5460`（`_validate_context_source_existence` 中的 `record_scope` 参数与边界契约）。而经对 Git 历史的精准追溯：
    ```bash
    git log -1 -S "record_scope" 4d1a1b18  # base commit
    ```
    发现该段逻辑早在 Base commit `4d1a1b18` 之前就已经存在，**在 PR #155 的实际提交（Head commit `034cf32b`）中，该行代码根本没有被修改！** 它属于 PR 之外的既有代码库实现。
  - 在既没有全量跨文件引用检索、Diff 又被截断、且目标代码本身未出现在 Diff 变更行中的情况下，Reviewer **根本没有物理可能** 获取判断该契约所需的事实输入。
  - **本次 Candidate 为何仍然未能检出**:
    本次 Candidate 严格遵循实验规范，仅修改 Prompt（单一变量控制），未修改 Context Retrieval / Assembly。因此 Type B 根本性缺失的信息依然未进入 Context。要解决 PR #155，后续必须通过跨文件符号追踪与上下文组装重构（Context Retrieval 阶段）来完成。

---

## 四、实验验收标准核对与判定结论

1. **条件 1: Contract TP >= 1**:
   - Baseline Contract TP = 0/2 (0.0%)
   - Candidate Contract TP = **1/2 (50.0%)** (PR #102 命中) -> **满足 (PASS)**
2. **条件 2: 总体 Grounded Recall > 30%**:
   - Baseline = 30.0%
   - Candidate = **40.0%** (+10.0%) -> **满足 (PASS)**
3. **条件 3: 原有三个 Grounded TP 不丢失**:
   - PR #107 (`herdr/scheduler.py`): matched_tp (保留)
   - PR #108 (`herdr/reverification.py`): matched_tp (保留)
   - PR #110 (`herdr/fix_loop.py`): matched_tp (保留)
   - PR #85 (`herdr/fix_loop.py`): novel_tp (保留)
   - 原有 TP 零丢失 -> **满足 (PASS)**
4. **条件 4: 不能出现明显 Precision 崩塌 (Augmented Precision >= 80%)**:
   - Baseline Augmented Precision = 87.5%
   - Candidate Augmented Precision = **90.0%** (+2.5%, 远高于 80% 门限)
   - Candidate Grounded Precision = **100.0%** -> **满足 (PASS)**

### 实验判定

```text
EXPERIMENT_SUCCESS
```

- **说明**: Candidate `rule-contract-v1` 在保持 100% Grounded Precision、无任何已有 TP 丢失、且 Augmented Precision 上升至 90.0% 的前提下，成功将 Grounded Recall 从 30.0% 提升至 40.0%，Contract Recall 从 0% 突破至 50.0%。
- **Baseline 处理**: 依照规则，保持 `haflow-review-baseline-v2` 作为冻结基线，不直接覆盖 Baseline，完整归档本次 A/B 实验数据。
