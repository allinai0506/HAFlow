# 工作流模板编写进阶实战指南 (Template Authoring Guide)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> 本文档指导开发者如何从零构建、调试和发布自定义的 HAFlow Workflow 模板。

---

## 1. 模板存放规范

HAFlow 支持在以下两级路径加载工作流模板：
1. **全局/用户模板目录**：`~/.herdr-controller/templates/<name>.yaml`
2. **仓库/内置模板目录**：`workflow_templates/<name>.yaml`

> 优先级：当两者重名时，用户目录的模板优先覆盖内置模板。

---

## 2. 模板结构快速骨架

```yaml
name: contract-review           # 唯一标识符，英文小写与横杠
label: 商业合同审查与风险评估   # 人类可读名称，展示在 UI/CLI
version: "1.0"                 # 版本号
description: 面向法务与商务的合同文本解析、合规条款初审与终审流程

nodes:
  - id: contract_ingest
    label: 合同文本解析与要素提取
    node_type: agent
    purpose: 提取合同主体、标的、金额与履行期限
    default_task_type: explore
    agent_policy:
      preferred:
        - claude
        - opencode

  - id: legal_compliance
    label: 法规与红线条款审查
    node_type: agent
    depends_on:
      - contract_ingest
    purpose: 审核违约责任、争议管辖与不可抗力条款
    default_task_type: review
    agent_policy:
      fixed: claude

  - id: business_risk
    label: 商务条款与付款风险对标
    node_type: agent
    depends_on:
      - contract_ingest
    purpose: 审查付款节点、账期风险及违约金比例
    default_task_type: review
    parallel: true

  - id: final_approval
    label: 综合审查意见汇总
    node_type: agent
    depends_on:
      - legal_compliance
      - business_risk
    purpose: 汇聚法务与商务意见，输出最终签字版审核报告
    agent_policy:
      preferred:
        - claude
    required_outputs:
      - docs/review/CONTRACT_AUDIT_REPORT.md
```

### 2.1 Context 执行模式骨架（非 Git 业务模板）

模板若不需要 Git 仓库/分支/提交（如报价、方案分析），用 `execution.mode: context`
声明运行环境，并用 `context` 声明所需业务上下文（字段契约见
[workflow-template-schema.md §1.1/§1.2](../product-specs/workflow-template-schema.md)）：

```yaml
name: context-smoke-test        # 已内置，可直接试跑
label: Context 执行链路冒烟
version: "1.0"
description: 验证 execution.mode=context 的启动、绑定与派发链路

execution:
  mode: context                 # 缺省为 git；context 不要求 Git 仓库

context:
  required:                     # 启动前必须绑定，缺失拒绝启动
    - id: common
      label: 公司通用资料
    - id: workspace
      label: 业务上下文目录
  optional:
    - customer

nodes:
  - id: analyze
    label: 上下文分析
    node_type: agent
    purpose: 读取绑定的上下文目录，产出分析结果
    default_task_type: docs
    default_integration_mode: none
    required_outputs:
      - 分析结果
```

运行方式（绑定路径属于运行期，绝不写进模板）：

```bash
cd <任意业务目录>          # context 模式不要求 Git 仓库
herdr-factory run "分析客户需求" --template context-smoke-test \
  --context common=/abs/company --context workspace=/abs/workdir
```

Context 引用是文件系统引用：目录或普通文件（Markdown/PDF/Word/Excel/JSON…）
都合法，例如 `--context contract=/contracts/福寿康.pdf`；不存在则拒绝启动。

同一业务 Workspace 可依次运行不同 context 模板（Workspace Identity !=
Workflow Template）：上一个 Workflow close 后再 run 新模板，Workspace 与
Coordinator 保留、Node Tabs 按新模板重建，context 契约与绑定完整延续。

context 模式下每个 Task 拥有独立 Task Workspace（Agent 只在其中写产物，
不会写入客户原始目录；context 路径是只读引用），无 CoW Clone/Branch/git
commit/integrate 路径；验收以 Task Workspace 产物与 verify-baseline 文件指纹为准。

---

## 3. 编写技巧与最佳实践

### 3.1 充分利用并发与汇聚
- 善用 `depends_on` 构建并行分支（如上述 `legal_compliance` 与 `business_risk` 在 `contract_ingest` 完成后会并发执行）。
- 汇聚节点（如 `final_approval`）只要把所有前置分支的 `id` 填入 `depends_on`，系统会自动挂起等待全部前置完成。

### 3.2 节点 Agent 策略设置建议
- **对严谨性要求高的节点**（如架构评审、合规审核、最终把关）：使用 `fixed: claude`。
- **对代码实现/并行搬砖要求高的节点**：使用 `preferred: [opencode, codex, qodercli]` 并设置 `parallel: true`。

### 3.3 核心开发模板选型决策指南

HAFlow 内置了两套核心软件开发工作流模板，面向不同复杂度的任务场景：

| 维度 | `code-craft-v1` (代码工坊) | `software-development-v1` (标准流程) |
|---|---|---|
| **核心架构** | 单工位 TDD 闭环 + 外环洁净室双门禁 | 6 阶段全面架构 (需求/计划/实现/测试/评审/收尾) |
| **节点数量** | 4 节点 (`craft` → `cleanroom_test` & `cleanroom_review` → `wrapup`) | 6 节点 (`requirements` → `plan` → `impl` → `test` & `review` → `wrapup`) |
| **协作模式** | 单 Agent 贯通开发，外环独立双门禁 | 需求与计划双角色对抗，实现支持解耦多并发 |
| **适用场景** | 需求边界清晰、单 Agent 可闭环、修改集中在少数模块 | 需求含隐式规则、跨模块改造、涉及架构拆分与多工位协作 |
| **典型任务** | 确定性 Bug 修复、局部 API 契约修复、局部重构、小功能完善 | 复杂业务功能、底层重构、前后端及数据库联动、大型迁移 |
| **返工目标** | `retry_node: craft` (回流至代码工坊) | `retry_node: implementation` (支持 Selective Replan) |

#### 选型指引建议：
- **优先选用 `code-craft-v1`**：当任务目标非常聚焦（例如根据已知测试报错修 Bug，或对已有类进行独立重构），无需启动多工位反复讨论需求和拆分任务，单工位 TDD 紧反馈配合外环洁净室验证最具性价比。
- **选用 `software-development-v1`**：当任务存在需求不确定性、涉及复杂的业务规则、或需要拆分成多个子任务由不同工位并发执行时，必须经过 `requirements` 和 `plan` 的双角色（主起草 + 对抗审查）推演，保障技术架构的稳健性。

### 3.4 门禁配置与交付契约规范
1. **显式声明 `gate`**：若节点属于验证或评审门禁（非默认的 `test`/`review`/`wrapup` 命名，如 `cleanroom_test`），必须显式声明 `gate: {retry_node: <target_node>}`，否则 Controller 无法将其识别为门禁节点，阻断自动裁决与 fix-loop 回流。
2. **门禁结论契约**：门禁节点的机器可采纳结论严格受限于 `HERDR_GATE_VERDICT: pass` 或 `HERDR_GATE_VERDICT: blocked`（二选一），严禁使用未经机器注册的自定义状态（如 `REWORK` 或 `FAIL`）。
3. **收尾节点规则**：收尾节点必须使用 `six-step-finish`，通过 `herdr-task create-pr` 建立交付 PR；严禁在工作流内自动执行 PR 合并，明确区分“工作流交付收尾”与“代码合入”。

### 3.5 模板本地调试流程
1. 将模板存入 `~/.herdr-controller/templates/my-template.yaml`；
2. 运行 `herdr-factory templates`，确认列表中能正常读取其标签、节点数量；
3. 运行自动化测试检查 DAG 逻辑：
   ```bash
   pytest -v tests/test_workflow_engine.py
   ```
4. 在测试项目中执行试跑：
   ```bash
   herdr-factory run "试跑测试任务" --template my-template
   ```

### 3.6 SPEC 规格开发与真实浏览器验收

`spec-development-v1` 为七节点 Git 模板：规格与澄清 → 方案与任务 → 实现与自查收敛 → 独立测试 / 独立审核并行 → 验收汇聚 → 六步收尾。规格节点内执行 specify、按需 clarify 及对抗检查；方案节点内执行 plan、tasks、analyze。审核节点依次要求 `ai-slop-cleaner --review` 和 `google-code-review`，只读验证同一冻结候选；修复交回实现节点，不在审核中移动候选。

三套研发模板（`code-craft-v1`、`software-development-v1`、`spec-development-v1`）的测试节点均要求启动冻结候选的本地服务，通过 `ego-browser` 技能执行真实业务 E2E，记录启动/版本、步骤、实际结果、浏览器及状态回读证据。未执行、跳过、失败或证据缺失不得输出 pass。测试使用隔离端口、临时数据和测试账号。

上述要求是模板的执行/验收契约，不是新增的 Controller E2E 回执校验器。用户目录同名模板优先于内置模板；更新源码后需核对实际加载路径。已有 Workflow 使用冻结定义，新模板不会追溯覆盖历史运行。
