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

### 3.3 模板本地调试流程
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
