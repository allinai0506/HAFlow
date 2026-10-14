# 企业经验中心 (Enterprise Experience Hub V1)

> **公司：上海共事智能科技有限公司**<br>
> **品牌：共事**<br>
> **产品：HAFlow**<br>
> **一句话：让人和多个 AI Agent 一起把事情做完**<br>
> *Human + Agent, in Flow*

---

## 1. 架构定位与业务目标

在企业多员工、多 Agent 协作网络中，Agent 主要运行在员工个人电脑（本地客户端）上。员工通过 Agent 完成业务工作时，会逐渐积累优质 Prompt、业务规则、工作方法和解决具体问题的经验。然而，这些经验往往停留在个人电脑、Agent 会话或具体任务中，无法自动转化为组织资产，其他员工不得不反复摸索。

**企业经验中心 (Enterprise Experience Hub V1)** 的核心目标：
**让员工 A 在 HAFlow 中形成、经过验证和审核的业务经验，可以被员工 B 在另一台电脑上的 HAFlow 自动复用。**

### 1.1 不可变更的架构约束

1. **纯内网私有化部署**：客户控制全部企业数据，不依赖云端 SaaS，不默认调用外部闭源商业模型。
2. **端云分离（边缘执行，中心共享）**：Agent 继续在员工个人电脑运行，企业 Hub 服务独立部署在客户内网服务器。
3. **隔离与不变量契约**：
   - 不修改 Herdr 核心调度语义、Agent 路由与任务状态机。
   - 不突破 Context Compiler 的 Run/Workflow 隔离契约。
   - 不把企业业务规则写入通用技能库 `universal-skills`。
4. **验证门禁 (Qualified Success Gate)**：经验自动发现必须以任务业务验收通过 (`qualified_success=True`) 且存在人工修正证据为前提，严禁从未经验证的任务或失败执行中猜测规则。
5. **审核发布门禁**：未经企业审核人员批准的候选经验，严格对其他员工隔离不可见。
6. **非阻塞与 Fail-Open**：经验提炼、网络同步或企业 Hub 停机故障，绝不阻塞员工正常的任务执行与结果结算。

---

## 2. 核心模块与拓扑架构

```
[ 员工 A 客户端 ]                                                [ 企业内网服务器 ]
┌─────────────────────────┐                                      ┌─────────────────────────┐
│ Herdr 执行现场           │                                      │ Experience Hub 服务     │
│ 任务结算 (qualified_success)                                   │ (bin/herdr-experience-hub)│
└────────────┬────────────┘                                      └────────────▲────────────┘
             │                                                                │
             ▼ 守护线程 (Fail-Open)                                            │ HTTP Bearer Token
┌─────────────────────────┐           HTTP POST /api/v1/experiences           │ (contributor / reviewer)
│ Local Collector         ├───────────────────────────────────────────────────┤
│ (已处理去重 + 离线缓冲)    │                                                   │
└─────────────────────────┘                                                   │
                                                                              ▼
                                                                 ┌─────────────────────────┐
                                                                 │ SQLite 企业规则库        │
                                                                 │ (experience_hub.db)     │
                                                                 │ - 待审核候选 (pending)   │
                                                                 │ - 已发布规则 (published) │
                                                                 │ - 审计日志 (audit_log)   │
                                                                 └────────────┬────────────┘
                                                                              │
                                                                              │ 审核人员批准 (published)
                                                                              │
[ 员工 B 客户端 ]                                                             │
┌─────────────────────────┐           GET /api/v1/experiences/rules          │
│ herdr-task 派发准备     │◄──────────────────────────────────────────────────┘
│ (客户上下文 + 身份凭证)   │
└────────────┬────────────┘
             │
             ▼ 注入 Prompt 上下文
┌─────────────────────────┐
│ Agent 执行现场 (工位)   │  自动具备企业已批准的业务规则，无需员工 B 重新摸索
└─────────────────────────┘
```

### 2.1 本地采集器 (`herdr/experience/collector.py`)
- **触发时机**：挂载于 `herdr/execution_outcome.py:try_autofinalize_for_task`。当任务完成结果结算且 `qualified_success=True` 时，由非阻塞守护线程异步唤醒采集。
- **本地去重**：维护客户端本地 SQLite 数据库 (`experience_collector.db`) 中的 `already_processed` 表，按 `task_id` + `run_id` 实现严格幂等。
- **离线缓冲队列**：网络不通或 Hub 服务暂时离线时，将提炼候选安全持久化至本地 `buffered_experiences`，网络连通后自动重试同步。

### 2.2 规则提炼核心 (`herdr/experience/extractor.py`)
- **纯函数设计**：遵循 Functional Core 架构，无 I/O 副作用。
- **触发门禁**：
  - 必须 `qualified_success=True`。任务执行失败、基线校验未过或未终态结算时，一律返回空列表。
  - 必须具备人工纠偏（`human_correction`, `human_steering`, `human_decision`, `rework`）事实。对于首次自运行即成功的普通任务，返回 `no_candidate`，不凭空捏造。
  - 过滤短小无价值指令（如“继续”、“重试”、“好的”等）。
- **敏感信息脱敏**：在客户端提取阶段即执行敏感模式识别（Bearer Tokens, API Keys, RSA 私钥, 密码等），统一替换为 `[redacted]`，防止企业涉密凭证出站。

### 2.3 企业共享服务端 (`herdr/experience/hub_server.py`)
- **独立服务**：基于 Python 标准库 `http.server` 与独立 SQLite (`experience_hub.db`) 构建，无第三方重型依赖。
- **认证与鉴权 (RBAC)**：
  - 请求必须携带 `Authorization: Bearer <token>`。
  - 角色划分：
    - `contributor`：仅支持提交规则候选 (`POST /api/v1/experiences`)。
    - `reviewer` / `admin`：支持查询待审候选、审核批准、驳回及撤回已发布规则。
- **客户边界隔离**：每个 Token 绑定其允许访问的客户清单 (`allowed_customers`)。跨客户访问严格阻断并记录审计。
- **全链路审计**：所有提交、审核、发布、撤回操作记录至 `experience_audit_log` 表，包含操作人、时间戳、动作与详情。

### 2.4 跨员工复用与派发注入 (`herdr/experience/client.py` & `bin/herdr-task`)
- 在 `bin/herdr-task:dispatch_task` 阶段，通过 `fetch_experience_rules_block` 向 Hub 查询符合当前客户 (`customer_id`) 与业务类型 (`business_domain`) 的有效已发布规则。
- 将已发布规则格式化为结构化提示块：
  ```markdown
  ### 企业业务规则 (Enterprise Business Rules)
  以下是由企业经验中心审核发布的强制业务规则，必须在本次任务中严格遵循：

  - [BR-xxx] 特殊报价规则 (v1)
    适用范围: 客户=c_acme_corp, 业务=quote, 任务=pricing
    规则内容: 对客户 Acme 批量报价时，若采购数量超过 1000 件，必须在阶梯单价基础上追加 5% 专项直降折扣。
  ```
- 优雅降级：Hub 不可用时，返回空规则块并记录警告，任务继续按原有基线派发，保障系统可用性。

---

## 3. 控制台审核入口 (`console/herdr_factory_console.py`)

在 HAFlow Factory 控制台中集成轻量审核控制面：
- **导航入口**：左侧一级导航栏增加 `企业经验 (Experience Hub)`。
- **待审核面板**：展示候选列表、来源任务/运行 ID、贡献员工、提取证据摘要与脱敏后的规则内容。
- **审核操作**：支持一键【通过发布】与【驳回】，发布后即时同步至服务端正式表。
- **已发布管理**：支持按客户/业务检索已生效规则，提供【撤回】操作，撤回后新任务即刻不再下发。

---

## 4. 关键验收与测试保障

测试覆盖 13 项关键测试用例 (`tests/test_experience_*.py`)：
1. 结算门禁：`qualified_success=False` 绝对不生成规则候选。
2. 证据充分性：无人工纠偏证据或普通简单指令时返回 `no_candidate`。
3. 凭证脱敏：敏感 Key 与密码在上传前全部洗脱为 `[redacted]`。
4. 本地幂等与离线恢复：重复触发不产生重复候选，离线缓冲持久化安全重试。
5. Hub 认证与 RBAC：匿名拦截 401，权限不足拦截 403。
6. 客户边界隔离：跨客户越权查询严格拦截。
7. 完整双客户端端到端闭环 (`test_full_enterprise_experience_lifecycle_dual_clients`)：
   - 员工 A 纠错 -> 提炼上传 -> 审核发布 -> 员工 B 自动读取注入并正确完成业务验收。
   - 证明规则真实进入 Agent 执行上下文并指导产出。
   - 员工 A 本地数据清空后，企业规则仍能被员工 B 稳定读取。
   - 规则撤回后新任务立即不再读取。
   - Hub 宕机时主任务优雅降级。
