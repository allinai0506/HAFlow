# Linear Connector V1 技术设计与实现前审查

状态：技术设计与实现前审查（Pre-implementation Review）  
基线：`e64cc3b7` (已完成 Inbox、Active Intake Core V1、Task Materialization V1)  
关联协议：[Active Intake V1 架构](active-intake-v1.md) · [Connector 契约规范](active-intake-v1-connector.md) · [Agent 工程协议](../engineering/agent-engineering-protocol.md)

---

## 1. 目标与非目标

### 1.1 核心目标
建立 HAFlow 第一个真实外部系统接入闭环，验证底层三层基础设施（Inbox、Active Intake Core、Task Materialization）与外部系统交互的可靠性：
1. **真实接入闭环**：`Linear (Issue 事件)` → `Linear Connector (验签/标准化)` → `Active Intake Core (持久化/去重/审计)` → `Task Materializer (物化为 Inbox 任务)` → `HAFlow Inbox (操作员查看/编辑/确认)`.
2. **零侵入核心**：严守架构边界，**不修改任何已冻结的 Active Intake Core 与 Materializer 核心代码**。Linear Connector 作为独立适配模块，严格遵循已发布的 Connector Protocol 接入。
3. **支持三个核心事件**：
   - **Issue 创建 (`create`)**：标准化为工作观察事实，经 EventStore 写入，Materializer 生成唯一 Inbox DRAFT 任务。
   - **Issue 更新 (`update`)**：标准化为更新事实，更新 `intake_objects` 快照，不重复创建任务，不覆盖操作员已编辑内容，审计 `intake_work_item_merged`。
   - **Issue 关闭 (`update` 转为已完成/已取消，或 `remove`)**：标记对象为 `CLOSED`，清空确认回执，使 Inbox 任务状态投影为 `SOURCE_CLOSED`，阻止新的编辑和确认，允许 unassign/ignore。

### 1.2 非目标（V1 明确排除）
- 暂不支持 Linear 评论（Comments）同步。
- 暂不支持 HAFlow 反向写回 Linear（仅单向 Ingress）。
- 暂不支持从 HAFlow 自动创建 Linear Issue。
- 暂不支持多用户外部身份映射（沿用单操作员受管映射策略）。
- 暂不支持自动触发并启动 Agent（必须经由操作员在 Inbox 中人工确认 Scope）。
- 暂不做周期性 Polling 轮询（首发采用 Webhook-first，Polling 作为后续补偿能力）。

---

## 2. 架构拓扑与真实调用链

```mermaid
flowchart TD
    subgraph Linear Cloud
        LI[Linear Issue Change] -->|HTTPS POST Webhook| WH[Linear Webhook Dispatcher]
    end

    subgraph HAFlow Ingress Boundary
        WH -->|Raw Bytes + Headers| EP["/api/intake/webhook/linear (Ingress Endpoint)"]
        EP --> VER["LinearVerifier (HMAC-SHA256 & Clock Skew)"]
        VER -->|VerifiedDelivery| NORM["LinearNormalizer (to CloudEvents 1.0)"]
    end

    subgraph Active Intake Core (Frozen)
        NORM -->|Delivery| ES["EventStore.accept_event()"]
        ES -->|BEGIN IMMEDIATE (WAL FULL)| DB[(State DB)]
        DB -->|Commit Receipt| ES
        ES -->|HTTP 200 OK| EP
        EP -->|HTTP 200 OK (<5s)| WH
    end

    subgraph Asynchronous Worker & Inbox (Frozen)
        W["services/herdr-intake.py (Worker)"] -->|claim_next_event| DB
        W --> MAT["materialize()"]
        MAT -->|Create Draft / Update Snapshot| TBL["intake_objects / intake_task_links / user_inbox_tasks"]
    end

    subgraph Operator Console
        UI["HAFlow Console (/inbox)"] -->|Read & Confirm| TBL
    end
```

### 真实调用链顺序与时限契约：
1. **Webhook 接收阶段（严格 < 5s 应答）**：
   - Linear 限制 Webhook 接收端必须在 **5 秒内** 返回 HTTP 200，否则判定失败并重试（最多 3 次）。
   - 入口端点获取原始请求体字节（限制最大 256KB），读取 `Linear-Signature`、`Linear-Timestamp`、`Linear-Delivery`、`Linear-Event` 请求头。
   - `LinearVerifier` 在常量时间内比对 HMAC-SHA256 签名，并核验时间戳偏差（<= 60s）。
   - `LinearNormalizer` 将供应商载荷转换为符合 CloudEvents 1.0 的规范 `Delivery`。
   - `EventStore.accept_event(delivery)` 在单一 SQLite 写事务中记录 `intake_delivery_receipts` 及 `intake_events`。
   - 持久化提交完成后，立即向 Linear 应答 `HTTP 200 OK`。**严禁在 Webhook 请求线程内执行外部网络补读、复杂耗时计算或任务物化**。
2. **异步物化阶段**：
   - 现有的后台 `herdr-intake.py` 服务通过 `claim_next_event` 原子领取事件。
   - 现有 `materializer.py` 识别 `com.haflow.intake.work-item.observed.v1`，并在写事务中更新对象状态、创建或更新任务链接。
3. **操作员处理阶段**：
   - 操作员在现有 Console Inbox 界面中查看外部工单，核对项目/工作区归属并执行 Assign、Edit、Confirm、Unassign 或 Ignore。

---

## 3. 授权方案与安全凭据体系

Linear 集成涉及两组不同权责的凭据，必须严格物理分离：

| 凭据类型 | 配置插槽 (`ref_slot`) | 用途 | 权限要求 | 存储与读取边界 |
| --- | --- | --- | --- | --- |
| **Webhook 签名密钥** | `signing_secret` | Webhook 入站请求签名验证 | 仅供验证签名，无 API 权限 | 存入受管环境或本地安全凭据文件，`auth_ref` 仅存引用键，严禁明文入库 |
| **API 访问令牌** | `api_read_token` | 连接健康检查、组织与团队元数据校验、可选的 Issue 详情补读 | 只读权限 (`read`) | 同上。对 Webhook 基础接收为可选，对配置预检与健康检查为必填 |

### 3.1 验签算法规范
- **算法**：HMAC-SHA256。
- **签名输入**：原始 HTTP 请求体未经解析的原始字节 (`raw_bytes`)。
- **密钥**：配置的 Webhook Signing Secret 字符串编码的 UTF-8 字节。
- **Linear-Signature 格式**：64 字符的小写十六进制字符串（注意：Linear 不带 `sha256=` 前缀，与 GitHub 不同）。
- **比对机制**：必须使用恒定时间比较函数 `hmac.compare_digest`，抵御针对签名的时序侧信道攻击。
- **时钟偏差限制**：校验 `abs(current_timestamp_ms - Linear-Timestamp) <= 60_000` 毫秒（60 秒），防止旧重放攻击。

### 3.2 组织身份硬绑定
- Linear Webhook Payload 中包含根级字段 `organizationId`。
- Connector 在接收时必须核对 Payload 中的 `organizationId` 是否与该 Connection 配置中的 `organization_id` **完全一致**。
- 若不一致，立即拒绝，记 `SOURCE_BINDING_MISMATCH`，绝不跨组织入账。

---

## 4. Linear Webhook 协议与生命周期

### 4.1 Linear HTTP 请求头规范
```http
POST /api/intake/webhook/linear HTTP/1.1
Host: haflow.example.com
Content-Type: application/json
Linear-Event: Issue
Linear-Delivery: 7c9e6679-7425-40de-944b-e07fc1f90ae7
Linear-Signature: 3c59a3bb96435bc20c5c464c502b4bb54de01e141a0210e7b7f1e7f95561570d
Linear-Timestamp: 1780747500000
User-Agent: Linear-Webhook
Content-Length: 1024
```

### 4.2 Linear Webhook Payload 结构
```json
{
  "action": "create",
  "type": "Issue",
  "createdAt": "2026-11-19T07:40:00.000Z",
  "webhookTimestamp": 1780747500000,
  "organizationId": "698c9f0b-2df8-43f1-b9ea-c3f2b6279f58",
  "data": {
    "id": "b1c2d3e4-f5a6-7b8c-9d0e-1f2a3b4c5d6e",
    "title": "Fix critical bug in authentication flow",
    "description": "Users are getting logged out randomly.",
    "identifier": "ENG-123",
    "priority": 1,
    "url": "https://linear.app/acme/issue/ENG-123",
    "state": {
      "id": "state-uuid-todo",
      "name": "Todo",
      "type": "unstarted"
    },
    "team": {
      "id": "team-uuid-eng",
      "key": "ENG",
      "name": "Engineering"
    },
    "assignee": {
      "id": "user-uuid-alice",
      "name": "Alice Smith"
    },
    "updatedAt": "2026-11-19T07:40:00.000Z",
    "createdAt": "2026-11-19T07:40:00.000Z"
  },
  "url": "https://linear.app/acme/issue/ENG-123",
  "webhookId": "wh_0123456789"
}
```

---

## 5. 事件标准化与字段清洗字典

### 5.1 CloudEvents 1.0 信封映射
Linear Connector 将验证通过的原始 Webhook 转换为标准 CloudEvents 1.0 信封：

| CloudEvents 属性 | 映射规则 | 说明 |
| --- | --- | --- |
| `specversion` | 固定 `"1.0"` | 平台标准 |
| `source` | `urn:haflow:intake:linear:{organizationId}:issue-webhook` | 必须与连接配置中的 `provider` + `provider_instance_key` + `capability` 精确匹配 |
| `id` | `f"issue:{data.id}:{version}"` | 业务事实唯一 ID。以 Issue UUID 结合毫秒版本号推导，保障同更新重投幂等、新更新唯一 |
| `type` | `"com.haflow.intake.work-item.observed.v1"` | 直接对接已冻结的 Task Materializer 核心类型 |
| `dataschema` | `"urn:haflow:intake:work-item:v1"` | 规范 Work Item 数据契约 |
| `subject` | `f"issue/{data.id}"` | 稳定外部对象引用，不含 PII |
| `time` | `data.updatedAt` 规范化为 RFC3339 UTC 字符串 | 确切的事实时间（如 `2026-11-19T07:40:00.000Z`） |
| `data` | 下节定义的规范 Work Item JSON 对象 | 经过字段防卫清洗的业务快照 |

### 5.2 双层去重机制（Delivery ID vs Event Fact ID）
1. **传输级去重 (`transport_id`)**：
   - 提取 `Linear-Delivery` 头（UUID）填入 `Delivery.transport_id`。
   - `intake_delivery_receipts.transport_id_digest` 保存其摘要，用于审计网络传输层面的重传。
2. **业务级事实去重 (`fact.event_id`)**：
   - 计算规则：`f"issue:{data.id}:{version}"`，其中 `version = int(datetime.fromisoformat(updatedAt).timestamp() * 1000)`。
   - 当 Linear 因网络波动重传相同修改时，`source`、`id` 与 `fact_digest` 完全相同，EventStore 返回 `outcome = "DUPLICATE"`，仅落 DUPLICATE 回执，不产生新事件，不进入消费队列。
   - 当 Linear 产生新的修改时，`updatedAt` 改变，`version` 递增，生成新的事实 ID，进入 EventStore 并被 Materializer 处理。

### 5.3 核心事件状态与 Action 映射规则
Linear 的 Issue 状态由 `data.state.type` 决定，包含 6 种类型：`backlog`, `unstarted`, `started`, `triage`, `completed`, `canceled`。同时 `action` 可为 `create`, `update`, `remove`。

映射逻辑矩阵：
```python
is_closed = (action == "remove") or (data.get("state", {}).get("type") in ("completed", "canceled"))

if is_closed:
    work_action = "closed"
elif action == "create":
    work_action = "work"
elif action == "update":
    work_action = "update"
else:
    work_action = "metadata"
```

| Linear 场景 | `action` | `data.state.type` | 映射为 HAFlow Action | HAFlow 系统行为 |
| --- | --- | --- | --- | --- |
| 创建活跃 Issue | `create` | `unstarted` / `backlog` / `started` / `triage` | `"work"` | EventStore 接收，Materializer 创建唯一 Inbox DRAFT 任务 |
| 创建已结 Issue | `create` | `completed` / `canceled` | `"closed"` | 记为关闭事实，直接归档，不创建任何新待办任务 |
| 更新活跃 Issue 内容 | `update` | `started` / `unstarted` 等未关闭状态 | `"update"` | 更新 `intake_objects` 快照，保留操作员已编辑内容，不重入待分配 |
| 关闭 Issue (Done) | `update` | `completed` | `"closed"` | 更新对象状态为 `CLOSED`，清空确认回执，使任务显示 `SOURCE_CLOSED` |
| 取消 Issue (Canceled) | `update` | `canceled` | `"closed"` | 行为同上，阻止后续编辑与确认 |
| 删除 Issue (Trash) | `remove` | 任意 | `"closed"` | 行为同上，安全阻断任务继续流转 |

### 5.4 严格字段清洗与防卫契约（Defensive Sanitization）
为防止外部不可控输入导致 Materializer 的 `task_text` 抛出 `ValueError` 并被记录为 `REJECTED: INVALID_WORK_ITEM`，Connector 必须在 Ingress 层强制清洗：

1. **空描述安全兜底（Critical）**：
   - Linear 的 `description` 允许为 `null` 或空字符串。
   - HAFlow 的 `task_text` 要求 `len(value.strip()) > 0`。
   - **清洗规则**：若 `description` 为空或纯空白，自动回退为：
     `f"Linear issue {data.identifier}: {data.title}\n\n(No description provided in Linear)"`。
2. **标题长度与前缀格式**：
   - 包含 identifier 前缀：`f"[{data.identifier}] {data.title}"`。
   - `task_text` 限制标题最大 240 字节。
   - **清洗规则**：若字节数超过 240，执行 UTF-8 字符边界安全截断至 237 字节并追加 `...`，并在描述顶部保留完整原始标题。
3. **描述长度上限保护**：
   - `task_text` 限制描述最大 8000 字节。
   - **清洗规则**：若超出 8000 字节，在 7900 字节处按行截断，并追加文本：`"\n\n... [Content truncated by Linear Connector (exceeded 8KB limit)]"`。
4. **控制字符剥离**：
   - 过滤 ASCII 码小于 32（保留 `\n`, `\r`, `\t`）及 127 (DEL) 的非法字符，防止 I-JSON 解析失败。
5. **身份信息归一化**：
   - 提取 `assignee` 字段：优先取 `data.assignee.name`，辅以 `data.assignee.id`。填入 `identities: {"assignee": "..."}`。

---

## 6. 连接配置与生命周期管理

### 6.1 Connector Manifest
遵循已冻结的 Connector Protocol，发布静态声明：

```yaml
manifest_version: "1"
provider: linear
connector_version: "1.0.0"
configuration_schema:
  type: object
  additionalProperties: false
  required: [organization_id, webhook_signing_secret_ref]
  properties:
    organization_id:
      type: string
      pattern: "^[a-zA-Z0-9_-]{1,64}$"
    webhook_signing_secret_ref:
      type: string
      pattern: "^[A-Za-z0-9_-]{1,128}$"
    api_token_ref:
      type: string
      pattern: "^[A-Za-z0-9_-]{1,128}$"
    team_keys:
      type: array
      items:
        type: string
        pattern: "^[A-Z0-9]{1,16}$"
auth:
  - method: webhook_hmac_sha256
    ref_slot: signing_secret
    scope: registered_organization_and_webhook
  - method: personal_api_key
    ref_slot: api_read_token
    scope: ["read"]
    purpose: object_read_and_health
    required: false
transports:
  webhook:
    ack_profile: linear_http_200_after_commit
    response_deadline_ms: 5000
    max_body_bytes: 262144
    signed_timestamp_max_skew_seconds: 60
object_read: true
events:
  - provider_type: "Issue:create"
    type: com.haflow.intake.work-item.observed.v1
    data_schema: urn:haflow:intake:work-item:v1
  - provider_type: "Issue:update"
    type: com.haflow.intake.work-item.observed.v1
    data_schema: urn:haflow:intake:work-item:v1
  - provider_type: "Issue:remove"
    type: com.haflow.intake.work-item.observed.v1
    data_schema: urn:haflow:intake:work-item:v1
health:
  checks: [verified_organization, last_delivery_receipt, read_grant_if_configured]
```

### 6.2 模块代码组织规范
所有代码置于独立子包 `herdr/intake/connectors/linear/`，保持与 Core 解耦：
```text
herdr/intake/connectors/linear/
├── __init__.py         # 导出 LinearConnector、LinearVerifier、LinearNormalizer
├── manifest.py         # 静态 Manifest 与 Schema 定义
├── verifier.py         # Webhook 签名与时钟检验逻辑
├── normalizer.py       # Linear Webhook -> CloudEvents / Work Item Fact 映射与清洗
├── client.py           # Linear GraphQL 只读客户端 (用于健康检查与测试连接)
└── connector.py        # 实现 Connector / WebhookConnector 接口协议
```

---

## 7. 真实端到端验收与验证方案

### 7.1 真实测试环境准备
1. **Linear 测试组织**：
   - 申请或使用现有的 Linear 免费测试 Workspace（例如 `haflow-lab`）。
   - 创建测试 Team（如 Key 为 `TEST`）。
   - 生成 Personal API Key（用于健康检查）。
2. **公网 Webhook 中继与端点暴露**：
   - 在开发和集成验收阶段，使用 **Cloudflare Tunnel / ngrok / smee.io** 将公网 Webhook 请求路由到本地开发服务端口（如 `http://localhost:8089/api/intake/webhook/linear`）。
   - 在 Linear 的 `Settings -> API -> Webhooks` 创建 Webhook 订阅，指向该穿透 URL，选择事件类型仅包含 `Issues`，并获取生成的 Signing Secret。
3. **HAFlow 本地受管配置**：
   - 将 `organization_id` 与 `signing_secret` 注入配置，注册 `linear` 连接。
   - 启动独立 Webhook 接收器或控制台端点。

### 7.2 端到端 7 步验收矩阵与断言

| 序号 | 步骤 | 真实操作与输入 | 预期现象与数据断言 | 涉及不变量与核验点 |
| :--- | :--- | :--- | :--- | :--- |
| **E2E-1** | **Issue 创建** | 在 Linear 中新建一个 Issue `TEST-101: Fix memory leak`，带详细描述 | 1. Webhook 收到 `create` 并在 5s 内返回 200 OK<br>2. `intake_delivery_receipts` 产生一条 `ACCEPTED` 回执<br>3. `intake_events` 插入一条状态为 `VERIFIED` 的规范正本<br>4. 后台 Worker 执行后状态转为 `PROCESSED`<br>5. Console Inbox 出现一条唯一的 `DRAFT` 任务 | 验证首次接入物化、无重复生成、标题前缀包含 `[TEST-101]` |
| **E2E-2** | **自动接收与审计** | 检查后台数据库表和审计日志 | 1. `intake_objects` 记录稳定 UUID、快照摘要与 `external_status = 'OPEN'`<br>2. `intake_task_links` 建立唯一链接<br>3. `events` 表记录 `intake_materialized` 审计 | 验证数据模型严格对应、无悬挂外键 |
| **E2E-3** | **修改 Issue** | 在 Linear 中将 `TEST-101` 标题改为 `Fix memory leak in buffer pool` 并修改描述 | 1. 收到 `update` Webhook，返回 200 OK<br>2. `intake_events` 产生新版本正本并消费完成<br>3. `intake_objects` 的快照被更新为最新版本<br>4. 审计记录 `intake_work_item_merged`<br>5. **Inbox 原任务不变，不生成第二条任务** | 验证更新幂等性、来源快照更新不冲掉操作员可能存在的草稿编辑 |
| **E2E-4** | **关闭 Issue** | 在 Linear 中将 `TEST-101` 拖动到 `Done` 状态 | 1. 收到 `update` (state.type = `completed`)<br>2. `intake_objects.external_status` 更新为 `CLOSED`<br>3. `intake_task_links` 的确认回执被清空<br>4. Inbox 界面中该卡片状态即刻变为 `SOURCE_CLOSED`<br>5. 界面编辑与确认按钮被禁用，仅允许 `unassign` 或 `ignore` | 验证外部终态与本地任务状态机的联动，阻断无效执行 |
| **E2E-5** | **重复投递 Webhook** | 使用相同 Payload、时间戳与签名，对端点发起重发 HTTP POST 请求 | 1. 端点立即返回 200 OK<br>2. `intake_delivery_receipts` 增加一条 `DUPLICATE` 回执，其 `canonical_event_id` 指向初次事件<br>3. `intake_events` 正本数量不变<br>4. Inbox 任务与状态完全无任何副作用 | 验证网络重试下的确定性幂等消费 |
| **E2E-6** | **异常载荷容错** | 在 Linear 中新建一个**无任何描述**、标题超长的 Issue | 1. Connector 自动填充兜底描述并安全截断标题<br>2. 正常物化为 DRAFT，无 `INVALID_WORK_ITEM` 抛错拒绝 | 验证外部不可控输入的鲁棒性清洗 |
| **E2E-7** | **连接撤权防护** | 将该 Linear 连接在 HAFlow 中标记为 `DISABLED` 或轮换 `config_revision`，再从 Linear 发起新 Issue | 1. Webhook 接收后识别连接失效，记 `REJECTED` 回执<br>2. 不产生任何新的可消费正本与任务<br>3. 原有已关闭或已存在的任务保持隔离不泄露 | 验证安全访问控制与授权边界隔离 |

---

## 8. 对抗式审查与关键风险防护

| 风险编号 | 潜在故障与攻击面 | 根本原因分析 | 预防与防御设计 |
| :--- | :--- | :--- | :--- |
| **R-01** | **空描述导致任务物化被拒** | Linear 允许 Issue 描述为 null，但 Materializer 的 `task_text` 强制非空且去除空白后必须有长度。若直接传递，Materializer 将因 `ValueError` 把事件置为 `REJECTED: INVALID_WORK_ITEM`。 | Connector 在 Normalizer 中强制注入兜底描述：`f"Linear issue {data.identifier}: {data.title}\n\n(No description provided)"`。 |
| **R-02** | **毫秒级高频更新版本冲突** | 若同一毫秒内快速触发两次 update，两者的 `version` (毫秒时间戳) 相等，导致 Materializer 判定版本冲突 (`OBJECT_VERSION_CONFLICT`)。 | 结合 Linear Webhook payload 提供的 `updatedFrom` 差异或者在版本号中加入纳秒/单调微计数 tie-breaker。 |
| **R-03** | **Webhook 5 秒超时重试风暴** | Linear 强制 5 秒超时，若 HAFlow 在接收线程内执行复杂网络请求或遇到写锁阻塞，会导致 Linear 不断重试产生风暴。 | 遵循 Transactional Inbox 模式：端点仅完成验签、解析、落库一条事件和回执，立即应答 200。所有物化与异步调用均交由后台独立 Worker 进程。 |
| **R-04** | **跨组织伪造与重放** | 恶意请求伪造合法签名，或利用合法签名向另一租户/连接的接收路径投递。 | 严格核验 payload 内的 `organizationId` 与配置的一致性，且每个连接拥有独立的 Signing Secret，验签不通过直接 401 拦截。 |
| **R-05** | **Issue 跨 Team 移动改变 Identifier** | 在 Linear 中把 Issue 从 ENG 移到 PRODUCT，其 Identifier 从 `ENG-10` 变为 `PROD-5`，但其内部 UUID `data.id` 保持不变。 | HAFlow 内部必须以 `data.id` (UUID) 作为 `external_id`。Identifier 仅作为标题展示前缀，确保实体坐标全局稳定。 |

---

## 9. 实施阶段分解与执行计划

- **第一阶段（基础设施）**：
  1. 实现 `herdr/intake/connectors/linear/` 模块（Manifest, Verifier, Normalizer, Client, Connector）。
  2. 编写针对验签、时间戳偏差、载荷清洗、版本映射的单元测试（100% 覆盖边缘分支）。
  3. 提供本地 Webhook 接收端点（在 Console 或独立服务 `services/herdr-intake-receiver.py`）。
- **第二阶段（事件接入与核心桥接）**：
  1. 将接收端点与 `EventStore.accept_event` 对接。
  2. 验证三个事件（Create, Update, Close/Remove）通过 `herdr-intake.py` 完整物化到 `user_inbox_tasks`。
  3. 模拟重投、空描述、跨版本并发的故障注入测试。
- **第三阶段（真实端到端验收与闭环）**：
  1. 配置真实 Linear 测试 Workspace 与 Webhook 订阅。
  2. 按照第 7.2 节的 7 步验收矩阵执行真实操作并收集证据。
  3. 产出实施与验收总结报告，更新 Wiki 与工程记录。
