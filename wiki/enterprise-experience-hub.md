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
- **两阶段可靠采集 (P1-01 / P1-02)**：任务完成结算时在主线程同步调用 `enqueue_task_settlement` 将任务以 `pending_collection` 状态写入本地 SQLite (`experience_collector.db`)，随后唤醒后台守护线程处理。即使触发进程立即退出，待处理采集任务也不会丢失。
- **自动恢复与指数退避 (P1-03)**：
  - Sentinel 守护进程 (`services/herdr-sentinel.py`) 周期性（每 30 秒）巡检并调用 `recover_and_sync`。
  - 任务派发前 (`bin/herdr-task`) 自动异步唤醒恢复。
  - 支持员工或运维脚本显式执行 `bin/herdr-task sync-experience` 强制同步。
  - Hub 离线或网络故障时自动进入 `pending_upload`，重试采用有界指数退避策略 (`min(300s, 5s * 2^retries)`)，避免风暴请求。
- **本地去重**：维护客户端本地 SQLite 数据库中的 `local_collector_records` 表，按 `task_id` + `run_id` 实现严格幂等。
- **数据库路径绝对化 (P2-01)**：入队时对显式与默认 `state_db_path` 统一执行 `abspath(expanduser())`（`_absolute_db_path`/`_resolved_db_str`），队列每行持久化其所属库的绝对路径；恢复以每行记录的库路径为准（`_row_state_db_path`），历史缺字段保留兼容回退，绝不允许跨 cwd 或路径解析失败把他人任务误判为永久丢失。
- **失败限定的 Run 隔离 (P2-02)**：`process_pending` 透传队列携带的 `queued_run_id`，终止失败、普通异常、重试退避三分支一致地只更新 `(task_id, run_id)` 对应记录；无法确定 Run 身份时拒绝批量修改同 Task 其他记录并安全返回，不影响 `uploaded` 终态保护与上传幂等。

### 2.2 规则提炼核心 (`herdr/experience/extractor.py`)
- **纯函数设计**：遵循 Functional Core 架构，无 I/O 副作用。
- **触发与证据门禁 (P1-08)**：
  - 必须 `qualified_success=True` 且 `verification_passed=True`。若 `outcome=None`，严格从 `execution_outcome` 权威事实层核查真实结算记录，禁止仅凭 Task 状态推断。
  - 必须具备有效的人工纠偏事实。决策文档中状态未解决 (`decision_status != "resolved"`)、作废或仅有人工问询的问题，一律拒绝转为业务规则。
  - 过滤短小无价值指令（如“继续”、“重试”、“好的”等），去重多条重复指令。
- **范围界定 (P1-06)**：未指定客户的任务统一标记为 `unknown`，严格禁止扩大为全局规则。
- **敏感信息脱敏**：在客户端提取阶段即执行敏感模式识别（Bearer Tokens, API Keys, RSA 私钥, 密码等），统一替换为 `[redacted]`，防止企业涉密凭证出站。

### 2.3 企业共享服务端 (`herdr/experience/hub_server.py`)
- **独立服务**：基于 Python 标准库 `http.server` 与独立 SQLite (`experience_hub.db`) 构建，无第三方重型依赖。
- **认证与凭据安全 (P0-01 / P1-09)**：
  - 删除所有硬编码默认凭证（如 `admin_secret_token_123`），旧库升级自动清除不安全默认 Token。
  - 首次启动强制通过 `--admin-token` 参数或 `HERDR_HUB_ADMIN_TOKEN` 环境变量初始化，未初始化时拒绝启动服务。
  - Token 落盘仅保存 SHA-256 哈希值，比对采用 `hmac.compare_digest` 恒定时间防时序攻击。
  - 严格禁止在 URL Query 参数中传递 Token。跨主机部署非回环地址强制配置 TLS 证书 (`--cert-file` 与 `--key-file`) 启用 HTTPS，或在内网反向代理后使用 `--allow-insecure-http` 明确授权。
- **请求体准入限制 (P1-01)**：`do_POST` 先认证再读体，认证失败直接 401 且不消费任何正文；统一字节预算 `MAX_REQUEST_BODY_BYTES = 1 MiB`（足够覆盖 V1 业务规则候选）；准入契约集中在 `_parse_body`/`_reject`：不支持 `Transfer-Encoding`→400、缺 `Content-Length`→411、非法/重复 `Content-Length`→400、超预算→413、截断体→400，拒绝路径显式关连接防止残体污染后续请求。
- **服务端强审与幂等 (P0-02)**：
  - 客户端提交新候选时，服务端强制设置 `status = "pending_review"`，贡献者身份绑定当前认证主体，`version = 1`，客户端无法越权发布。
  - 相同 ID 重复提交时校验内容哈希 (`content_hash`)：相同内容幂等返回 200，不同内容返回 409 Conflict。
- **候选隔离与越权防范 (P1-04 / P1-05)**：
  - 普通贡献者 (`contributor`) 在候选列表和详情接口中仅允许查看自身提交的待审候选，禁止窥探其他员工的未审核修正内容。
  - 审核人员 (`reviewer`) 执行审核、驳回或撤回前，服务端先行获取目标经验的客户归属，严格核对操作人的 `allowed_customers`，跨客户操作一律返回 403 Forbidden。
- **全链路审计**：所有提交、审核、发布、撤回操作记录至 `experience_audit_log` 表，包含操作人、时间戳、动作与详情。

### 2.4 跨员工复用与派发注入 (`herdr/experience/client.py` & `bin/herdr-task`)
- 在 `bin/herdr-task:dispatch_task` 阶段，通过 `fetch_experience_rules_block` 向 Hub 查询符合当前客户 (`customer_id`) 与业务类型 (`business_domain`) 的有效已发布规则。
- **强制规则 Fail-Closed 保证 (P1-07)**：
  - 区分普通任务与强制业务规则任务 (`mandatory_experience_rules=True`)。
  - 当任务要求强制业务规则时，若 Hub 离线、Token 失效或网络异常，客户端与 CLI 抛出 `RuntimeError` 拒绝派发，严格执行 Fail-Closed；普通任务记录告警并优雅降级。

---

## 3. 控制台审核入口 (`console/herdr_factory_console.py`)

在 HAFlow Factory 控制台中集成轻量审核控制面：
- **导航入口**：左侧一级导航栏增加 `企业经验 (Experience Hub)`。
- **安全鉴权**：清理控制台硬编码管理员 Token，支持通过环境变量或请求头透传认证凭据。
- **错误透传与离线提示**：经验中心不可用或鉴权失败时，前端展示明确错误状态横幅，禁止静默掩盖为“暂无数据”。
- **待审核面板与操作**：展示候选列表、来源任务/运行 ID、贡献员工、提取证据摘要与脱敏后的规则内容，支持【通过发布】与【驳回】。
- **已发布管理**：支持按客户/业务检索已生效规则，提供【撤回】操作，撤回后新任务即刻不再下发。

---

## 4. 关键验收与测试保障

测试覆盖 24 项关键测试用例 (`tests/test_experience_*.py`)：
1. **凭证初始化门禁**：无管理员凭证初始化时，Hub 生产启动被拒绝。
2. **发布防伪造**：普通贡献者提交 `status="published"`，服务端强转为 `pending_review`。
3. **语义冲突检测**：相同 ID 提交不同内容返回 409 Conflict。
4. **候选隔离**：普通贡献者无法列出或获取其他贡献者的未审核候选（列表过滤，详情 403）。
5. **跨客户审核/撤回拦截**：非授权客户的 Reviewer 执行审核或撤回返回 403 Forbidden。
6. **未知客户隔离**：客户为 `unknown` 的规则不向特定客户扩散，亦不默认视为全局。
7. **URL 凭证封禁**：URL Query 传递 Token 拦截 401。
8. **可靠采集入库**：任务结算在主线程同步写入 `pending_collection`，守护线程中断或进程崩溃后经重启自动恢复。
9. **指数退避重试**：离线重试按退避窗口调度，强制同步跳过等待窗口。
10. **决策严密性**：未解决决策及空泛问询被严格过滤，已解决有效决策正确转为规则。
11. **强制规则 Fail-Closed**：`mandatory_experience_rules=True` 任务在 Hub 离线或认证失败时抛出异常终止派发。
12. **完整双客户端端到端闭环**：真实还原员工 A 纠错 -> 采集上传 -> 审核发布 -> 员工 B 自动读取注入并正确完成业务验收。员工 A 本地清理后规则持久有效，规则撤回后实时停用。
