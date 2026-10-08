# HAFlow 代码评审回归评测工具指南 (Review Benchmark)

> 为 HAFlow 建立基于真实历史缺陷与 GitHub ReviewBench 官方评分体系的代码评审能力评测。

---

## 1. 架构与设计原则

Review Benchmark 是一个用于评测 AI Agent 对 HAFlow 代码审查能力的独立开发工具。
评测遵循四大纪律：
1. **真实案例与铁证支持**：评测集全部取自已修复、具备精确 commit SHA 和回归测试证明的历史缺陷案例，绝不凭空编造。
2. **代码现场物理隔离**：每个案例运行在独立的 Git 临时工作树中，被测 Agent 只能获取当时的上下文和被审改动，避免历史记录和上下文缓存泄漏参考答案。
3. **复用 ReviewBench 官方评分契约**：锁定 ReviewBench 官方提交 `e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d`，直接复用其问题匹配器与评分公式，支持官方 Grounded & Augmented Recall / Precision 计算。
4. **状态真实与明确区分**：严格区分“审核完成未发现问题”、“Agent 启动失败”、“执行超时”与“输出解析失败”，失败情况绝不隐式降级为 0 问题。

---

## 2. 评测数据集 (Corpus & Golden Set)

内置 3 个经过真实验证的 HAFlow 缺陷案例，保存在 `tests/fixtures/review_benchmark/`：

| 案例 ID | PR | Base SHA | Head SHA | 缺陷类型 | 验证证据 |
|---|---|---|---|---|---|
| `allinai0506_HAFlow_107-3477d064` | #107 | `a957663a` | `3477d064` | `extract_task_candidate_sha` 未校验克隆真实 baseline_commit，导致声明与客观证据不一致的候选通过汇合门禁 | `e6408e7df2b3` (P1-2 修复) / `tests/test_scheduler_dispatch_e2e.py` |
| `allinai0506_HAFlow_110-2b9f2177` | #110 | `79d54abd` | `2b9f2177` | `verdict_fingerprint` 包含易失 `task_id`，后继代任务生成新 ID 导致 repeat 检测失配，死循环扣减预算 | `291c1f75987f` (PR #158 修复) / `tests/test_fix_loop_recovery.py` |
| `allinai0506_HAFlow_108-7709da70` | #108 | `3a846590` | `7709da70` | `decision_identity` 未绑定候选冻结 episode ID，导致工作流回滚复活历史旧复用事实 | `2879a1ff1e0f` (PR #108 审修复) / `tests/test_reverification_v1.py` |

---

## 3. 安装与前置依赖

### 依赖环境
- Python 3.10+
- Node.js 18+ 与 npm
- 可选：Review 审查 Agent（如 `agy`、`opencode`、`pi` 等）
- 可选：ReviewBench 裁判模型 API Key（如 `DEEPSEEK_API_KEY`、`ANTHROPIC_API_KEY` 等）

ReviewBench 评分器自动克隆并锁定到 `~/.cache/review-bench-src`：
```bash
git clone https://github.com/review-bench/ReviewBench ~/.cache/review-bench-src
cd ~/.cache/review-bench-src
git checkout e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d
npm ci
```

---

## 4. 命令使用规范

统一使用 `bin/herdr-review-bench` 驱动评测流程：

### 4.1 评测当前审核方式 (生成基线)
```bash
bin/herdr-review-bench run \
  --config tests/fixtures/review_benchmark/baseline.json \
  --output /tmp/haflow-review-bench/baseline
```
- 输出目录包含：
  - `candidate/`: 被测 Agent 的标准化 JSON 评审意见
  - `scoring/results.json`: 官方评分器指标聚合结果
  - `scoring/results.details.json`: 逐条意见状态判定明细
  - `report.md`: 中文 Markdown 完整评审报告
  - `results.json`: 顶层快速复核结果

### 4.2 评测改进版配置
调整评审提示词（Prompt）、上下文组装策略或模型配置后：
```bash
bin/herdr-review-bench run \
  --config tests/fixtures/review_benchmark/candidate.json \
  --output /tmp/haflow-review-bench/candidate
```

### 4.3 生成两次评测的对比报告
```bash
bin/herdr-review-bench compare \
  --before /tmp/haflow-review-bench/baseline \
  --after /tmp/haflow-review-bench/candidate \
  --output /tmp/haflow-review-bench/comparison.md
```
对比报告自动计算 Recall 与 Precision 的 Delta 变化矩阵及趋势提示。

### 4.4 自动化 PR 审核闭环 (`auto-pr-review`)
在 CI 流水线或本地针对指定 PR 执行完整的规则审核、影子审核、Fact-Check 与工件生成：
```bash
python3 bin/herdr-review-bench auto-pr-review \
  --repo . \
  --base "<BASE_SHA>" \
  --head "<HEAD_SHA>" \
  --pr-number "<PR_NUMBER>" \
  --pr-title "$PR_TITLE" \
  --pr-body "$PR_BODY" \
  --output-dir ".omc/review_artifacts" \
  --github-token "$GITHUB_TOKEN" \
  --repo-slug "allinai0506/HAFlow" \
  --agent "rule" \
  --shadow-agent "agy" \
  --timeout 180
```
核心产出：
- 生成 4 项标准审计工件至 `--output-dir`：`review-result.json`、`shadow-review.json`、`context-audit.json`、`finding-verification.json`。
- 自动向 GitHub PR 提交或原地更新单例审核评论（携带 HEAD SHA 防过期覆盖保护）。

---

## 5. 配置文件说明 (`baseline.json`)

```json
{
  "name": "baseline-agy",
  "agent": "agy",
  "judge": {
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d"
  },
  "benchmark": {
    "manifest": "tests/fixtures/review_benchmark/corpus/manifest.json",
    "golden_dir": "tests/fixtures/review_benchmark/golden"
  },
  "review": {
    "timeout_seconds": 300,
    "system_prompt": "You are a senior code reviewer for HAFlow. Review the given git diff against the codebase and find defects, security flaws, and regression bugs. Return findings strictly in JSON format."
  }
}
```

---

## 6. 自动化测试与质量门禁

无需外部 API 调用的本地测试覆盖：
```bash
pytest -q tests/test_review_benchmark.py
```
覆盖项：
- ReviewBench Candidate 契约 JSON 格式校验与行号自洽
- SHA 错配与缺少必要字段检测
- Agent 超时、启动失败、格式错误与正常 0 问题区分
- 中文报告渲染与差异计算
