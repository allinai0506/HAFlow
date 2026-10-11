# HAFlow Review Benchmark 评测体系与上下文对照机制 (review-benchmark.md)

> **公司：上海共事智能科技有限公司**<br>
> **品牌：共事**<br>
> **产品：HAFlow**<br>
> **一句话：让人和多个 AI Agent 一起把事情做完**<br>
> *Human + Agent, in Flow*<br>
> 本页面定义 HAFlow 代码审查基准评测（ReviewBench）的设计契约、同模型单变量对照机制与真实用量审计规范。

---

## 1. 定位与核心原则

Review Benchmark 是验证 AI Reviewer 缺陷检出能力与代码审查质量的基准评测体系。
系统严格遵循四大评测纪律：
1. **真实历史案例与铁证支撑**：所有评测 PR 均取自 HAFlow 真实已修复的缺陷（含精确 commit SHA 与回归测试证明）。
2. **同模型单变量对照契约**：对比评测必须严格控制单一变量（如源码上下文供给模式）；必须验证评审 Agent 与模型版本真实一致，未确认模型时严禁输出同模型优劣结论。
3. **基础 Diff 绝对一致与防挤占**：基线组（`diff_only`）与候选组（`existing_four_tier`）必须看到字符级完全一致的基础 diff；扩展上下文只在基础 diff 之后追加，硬预算下严禁反向挤出基础变更。
4. **状态透明性与三态追踪**：显式区分“四层成功”、“主动 diff_only”与“检索异常降级”；降级样本必须携带 `fallback_reason`，严禁伪装为成功执行。
5. **真实 Token 防猜测**：Token 用量仅采纳 API 真实统计结果；缺失时保留“未知”，严禁通过字符数折算，不把缺失成本记为零。

---

## 2. 上下文模式与配置契约

系统在 `review` 配置中提供上下文模式选项：

```yaml
review:
  context_mode: diff_only          # 模式：diff_only 或 existing_four_tier (默认)
  context_budget_chars: 35000     # 上下文预算上限字符数 (默认 35000)
  timeout_seconds: 300
```

### 2.1 模式语义
- `diff_only`：仅提供 15,000 字符保护上限内的基础 diff 与必要 PR 元信息，不触发四层源码检索。
- `existing_four_tier`：在相同基础 diff 之后追加 AST 符号定义、调用依赖与契约源码；由 `context_budget_chars` 限制组装总长度。
- **降级路径**：当四层检索抛出异常或缺失 git 仓库时，自动降级为 `diff_only`，记录 `fallback_reason`，并在对比报告中触发可比性告警。

---

## 3. 单变量可比性审计矩阵

对比报告通过 `check_benchmark_comparability` 自动审计以下条件：

| 对照条件 | 校验规则 | 违背处置 |
|---|---|---|
| Reviewer Agent | 两组 `agent` 必须一致 | 标记 `⚠ 存在差异`，阻断结论输出 |
| 模型身份 | 整组所有尝试有效确认同模型且无运行时漂移 (`model_confirmed=True`) | 标记 `⚠ 无法确认同模型`，阻断严格结论输出 |
| CLI 版本证据 | 外部 CLI Reviewer 的 CLI 版本必须已知且一致，缺失或 unknown 时仅阻断严格对照归因 | 标记 `⚠ 缺少 CLI 版本有效证据 / 存在差异`，阻断严格结论输出 |
| 共同提示词指纹 | 逐样本共同提示词指纹 (`common_prompt_fingerprint`) 必须一致（涵盖系统指令、固定模板、PR title 与 PR body，排除上下文变化） | 标记 `⚠ 共同提示词指纹不一致 / 缺失`，阻断严格结论输出 |
| 黄金标准内容指纹 | 裁判 `--golden` 目录文件集与内容 SHA-256 指纹 (`golden_fingerprint`) 必须一致且存在 | 标记 `⚠ 评测裁判黄金标准内容指纹不一致 / 缺失`，阻断严格结论输出 |
| 对照方向 | 严格限制为基线 `diff_only` → 改进版 `existing_four_tier` | 标记 `⚠ 对照方向不符合标准要求`，阻断结论输出 |
| 系统提示词/指令 | 两组默认值解析后的有效提示词指纹 (`system_prompt_fingerprint`) 必须一致 | 标记 `⚠ 系统提示词/指令不一致`，阻断结论输出 |
| 执行超时 | 两组有效 `timeout_seconds` 必须相等 | 标记 `⚠ 超时时间不一致`，阻断结论输出 |
| 基础 Diff | 字符级长度及 SHA-256 内容指纹 (`base_diff_fingerprint`) 必须一致 | 标记 `⚠ 基础 diff 内容/长度不一致`，阻断结论输出 |
| 上下文预算 | 启动前经 `validate_context_config` 严格原始类型校验（排除布尔与浮点数），两组 `context_budget_chars` 必须相等 | 标记 `⚠ 存在差异`，阻断结论输出；非法配置立即以退出码 1 失败拦截 |
| 案例完整性 | 样本数量、顺序、Base/Head SHA 必须 1 对 1 吻合，且与计划数一致 | 标记 `⚠ 存在差异或失败`，阻断结论输出 |
| 降级样本 | 两组 `fallback_count` 必须为 0 | 标记 `⚠ 包含降级样本`，不可作为四层收益证据 |
| 用量与耗时分母 | 统一以进入 `run_agent_review()` 为尝试界限，排除工作区准备失败 | 标记部分覆盖 `(已知部分合计: x/y 样本)`，杜绝填零伪造 |

---

## 4. 证据契约与核心源码

Evidence:
- `herdr/review_benchmark.py#VALID_CONTEXT_MODES`
- `herdr/review_benchmark.py#validate_context_config`
- `herdr/review_benchmark.py#extract_base_diff`
- `herdr/review_benchmark.py#compute_golden_fingerprint`
- `herdr/review_benchmark.py#resolve_agent_identity`
- `herdr/review_benchmark.py#aggregate_group_agent_identity`
- `herdr/review_benchmark.py#check_benchmark_comparability`
- `herdr/review_benchmark.py#compare_benchmarks`
- `bin/herdr-review-bench:cmd_run`
- `bin/herdr-review-bench:cmd_review`
- `tests/test_review_benchmark.py#TestReviewBenchContextModes`
- `tests/test_review_benchmark.py#TestGroupModelIdentityTable`
- `tests/test_review_benchmark.py#TestTokenCoverageAndTimingContracts`
- `tests/test_review_benchmark.py#TestRealCallChainIntegration`
- `tests/test_review_benchmark.py#TestAcceptanceComparabilityAndBudgetTables`
