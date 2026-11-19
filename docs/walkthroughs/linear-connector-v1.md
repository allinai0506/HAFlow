# Linear Connector V1 实施与测试闭环报告

状态：已完成第一步（Connector 基础设施）与第二步（事件接入与核心桥接），等待第三步真实环境端到端操作  
分支：`feat/linear-connector-v1`  
关联设计：[linear-connector-v1.md](../architecture/linear-connector-v1.md)

---

## 1. 实施概览

本项目严格遵循「零侵入既有核心」原则，**未修改任何已冻结的 Active Intake Core 与 Task Materializer 代码**（`herdr/intake/store.py`、`protocol.py`、`materializer.py`、`worker.py` 均保持不变）。

通过独立的 Connector 适配模块 `herdr/intake/connectors/linear/`，成功打通了从 Linear Webhook 到 HAFlow User Inbox 的全流程闭环：

```text
Linear Cloud (Issue 事件)
    ↓ HTTPS Webhook POST
Ingress 端点 (services/herdr-linear-webhook.py 或 console /api/intake/webhook/linear)
    ↓ 验签与时钟核验 (LinearVerifier)
    ↓ 载荷防卫清洗与标准化 (LinearNormalizer)
EventStore (accept_event -> SQLite WAL FULL 事务提交 -> 200 OK)
    ↓ 异步消费 (services/herdr-intake.py Worker)
Task Materializer (materialize -> intake_objects / intake_task_links / user_inbox_tasks)
    ↓ 状态投影与操作员处理
HAFlow Console User Task Inbox
```

---

## 2. 交付模块清单

| 文件路径 | 职责与能力 |
| :--- | :--- |
| `herdr/intake/connectors/linear/manifest.py` | 静态 Manifest 声明、配置验证规则（`validate_config`） |
| `herdr/intake/connectors/linear/verifier.py` | HMAC-SHA256 签名验证（`Linear-Signature` bare hex）、时钟偏差检查（<= 60s） |
| `herdr/intake/connectors/linear/normalizer.py` | Linear Issue 载荷清洗与转换：`create` -> `work`, `update` -> `update`, `completed`/`canceled`/`remove` -> `closed`；空描述安全回退、超长标题/描述截断、控制字符剥离 |
| `herdr/intake/connectors/linear/client.py` | Linear GraphQL 只读客户端，支持 `test_connection` 与 `health_check` |
| `herdr/intake/connectors/linear/connector.py` | `LinearConnector` 主控类，组合 Verifier、Normalizer 与 Client |
| `herdr/intake/connectors/linear/receiver.py` | Webhook HTTP 处理函数 `handle_linear_webhook` 及 `LinearWebhookHandler` |
| `services/herdr-linear-webhook.py` | 独立运行的 Webhook Ingress CLI 守护服务 |
| `console/http_server.py` & `console/inbox_api.py` | 控制台可选集成的 `/api/intake/webhook/linear` 路由桥接 |
| `tests/test_linear_connector.py` | 8 项单元与集成测试套件，覆盖完整生命周期与边缘分支 |

---

## 3. 验证铁证

### 3.1 Linear Connector 专属测试（8/8 全部通过）
```bash
pytest -v tests/test_linear_connector.py
```
```text
tests/test_linear_connector.py::test_manifest_and_config_validation PASSED [ 12%]
tests/test_linear_connector.py::test_verifier_valid_and_invalid_signatures PASSED [ 25%]
tests/test_linear_connector.py::test_normalizer_issue_actions PASSED     [ 37%]
tests/test_linear_connector.py::test_normalizer_defensive_sanitization PASSED [ 50%]
tests/test_linear_connector.py::test_linear_client_test_connection_and_health PASSED [ 62%]
tests/test_linear_connector.py::test_end_to_end_linear_issue_lifecycle PASSED [ 75%]
tests/test_linear_connector.py::test_receiver_http_handler_dispatch PASSED [ 87%]
tests/test_linear_connector.py::test_console_api_linear_webhook_endpoint PASSED [100%]
============================== 8 passed in 0.37s ===============================
```

### 3.2 全量 Intake 与 Connector 回归测试（70/70 全部通过）
```bash
pytest -q tests/test_intake_*.py tests/test_linear_connector.py
```
```text
......................................................................   [100%]
70 passed in 38.05s
```

### 3.3 静态与代码风格检查
```bash
python3 -m compileall -q herdr services console tests
git diff --check
python3 services/herdr-linear-webhook.py --help
```
编译无任何语法错误，无空白违规，CLI 正常输出。

---

## 4. 关键排障与防卫设计验证点

1. **空描述安全兜底（防 `INVALID_WORK_ITEM` 崩溃）**：
   - Linear 允许 Issue 描述为 `null`，而 Materializer 的 `task_text` 严格要求非空。
   - `test_normalizer_defensive_sanitization` 与 `test_end_to_end_linear_issue_lifecycle` 验证：空描述被安全转换为 `Linear issue TEST-102: Issue with empty description\n\n(No description provided in Linear)`，成功进入 Inbox 作为 DRAFT 任务，彻底消除了崩溃隐患。
2. **Issue 状态联动与关闭阻断**：
   - 验证了当 Issue 变为 `completed` 或 `canceled` 或 `remove` 时，状态自动置为 `CLOSED`，清空确认回执，Inbox 状态投影为 `SOURCE_CLOSED`。
   - 验证了状态机严格阻断对已关闭事项的 Confirm 操作。
3. **5 秒超时保护与 Transactional Inbox**：
   - 验证了 Webhook HTTP 处理函数仅在事务内完成正本与回执落库，立即应答 200 OK，与后台异步物化解耦。
4. **重传幂等性**：
   - 验证了完全相同的 Webhook 重新投递时，EventStore 返回 `DUPLICATE`，不增加正本，不重复生成任务。

---

## 5. 第三步：真实端到端验收操作指南

当准备好真实的 Linear 测试 Workspace 时，可按以下步骤操作：

### 5.1 启动服务
1. **启动后台 Intake 消费 Worker**：
   ```bash
   python3 services/herdr-intake.py --tenant tenant --connection conn-linear
   ```
2. **启动 Webhook Ingress 端点**：
   - 方式 A（独立服务，监听 8089 端口）：
     ```bash
     python3 services/herdr-linear-webhook.py \
       --tenant tenant \
       --connection conn-linear \
       --org-id <your-linear-org-id> \
       --secret <your-webhook-signing-secret> \
       --port 8089
     ```
   - 方式 B（随控制台启动，监听控制台端口）：
     设置环境变量后启动控制台即可：
     ```bash
     export LINEAR_ORGANIZATION_ID="<your-linear-org-id>"
     export LINEAR_WEBHOOK_SECRET="<your-webhook-signing-secret>"
     export HAFLOW_INTAKE_TENANT="tenant"
     export HAFLOW_LINEAR_CONNECTION="conn-linear"
     ./console/run-console.sh
     ```

### 5.2 配置公网转发与 Linear Webhook
1. 使用 `ngrok http 8089` 或 `cloudflared tunnel` 生成公网 HTTPS 地址。
2. 在 Linear 的 `Settings -> API -> Webhooks` 点击 `New Webhook`：
   - **URL**：`https://<your-tunnel-domain>/api/intake/webhook/linear`（或独立端口的 `/`）
   - **Event Types**：勾选 `Issues`
   - **Secret**：复制生成的 Signing Secret，填入启动环境变量。

### 5.3 按照 7 步矩阵执行真实操作
1. **新建 Issue**：在 Linear 创建 `TEST-1`，检查控制台 Inbox 出现 DRAFT 任务。
2. **核对快照**：点击 Inbox 卡片查看来源详情与外部 ID。
3. **编辑 Issue**：在 Linear 修改标题，检查 Inbox 来源快照刷新且原草稿不重复。
4. **关闭 Issue**：在 Linear 拖动到 Done，检查 Inbox 卡片变为 `SOURCE_CLOSED`。
5. **重试投递**：在 Linear Webhooks 页面点击 `Redeliver`，检查 HAFlow 返回 200 且无多余任务。
6. **空描述测试**：在 Linear 新建无描述 Issue，检查正常入箱。
7. **连接管理**：更新连接状态为 DISABLED，验证新事件被隔离。

---

## 6. PR Review 修复记录 (Codex P1 / P2)

- **Codex P1 (Resource Exhaustion Prevention)**:
  - 在 `LinearWebhookHandler` 和 `console/http_server.py` 中，在调用 `rfile.read()` 之前增加 `Content-Length <= 256KB` 刚性上限校验与 socket 5.0s 超时；超长或非法长度请求直接响应 413 / 400 并关闭连接，避免拒绝服务风险。
- **Codex P2 (Removal Fact Identity Collision)**:
  - Linear 在发送 `remove` 事件时，`data.updatedAt` 可能会保留删除前的旧版本时间戳。若 CloudEvents ID 仅采用 `issue:{id}:{version}`，会导致删除事实与此前版本的 Fact ID 冲突而被 `EventStore` 判定为 `FACT_IDENTITY_COLLISION` 拒绝。
  - 修复：CloudEvents ID 引入生命周期动作维度 `issue:{id}:{action}:{version}`；并在删除事件中优先以 `payload.createdAt` 作为生命周期事实版本时间戳，保障删除事实被正常接收并使任务进入 `SOURCE_CLOSED`。
- **回归测试覆盖**：
  - `tests/test_linear_connector.py::test_codex_p1_content_length_limit_enforced_before_read`
  - `tests/test_linear_connector.py::test_codex_p2_remove_with_same_updated_at_does_not_collide`

---

## 7. 复审修复记录 (3 P1 + 1 P2)

### 7.1 P1-01：Webhook 签名 Body 时间戳校验与防重放
- **根因**：原 `LinearVerifier` 仅检查了未包含在 HMAC 摘要保护范围内的 HTTP Header `Linear-Timestamp`，攻击者可截获历史合法请求体后伪造当前 Header 时间戳绕过窗口校验。
- **修复**：
  - HMAC 摘要验证通过后，在解析出的签名 Body 中强制校验 `webhookTimestamp`。
  - 使用经过签名保护的 `webhookTimestamp` 执行 60s 钟差新鲜度检查；若缺失或钟差超限立即阻断。
  - Header 中的 `Linear-Timestamp` 仅作为辅助比对，若与签名 Body 差异超限亦直接阻断。
- **测试**：`tests/test_linear_connector.py::test_verifier_valid_and_invalid_signatures`。

### 7.2 P1-02：连接授权版本动态传递与非静默拒绝
- **根因**：两个 Webhook HTTP 入口原先以硬编码 `config_revision=1` 构造 Connector，当外部连接执行授权变更（如 1 -> 2: DISABLED -> 3: ACTIVE）时，Connector 仍提交旧版本导致事件被拒绝；且 `handle_linear_webhook` 原先对 REJECTED 错误静默返回 200，造成外部误判成功。
- **修复**：
  - 在 `receiver.py` 增加 `get_connection_config(store)`，每次入库前从 State DB 可信读取当前连接状态与 `config_revision` 并注入 Connector。
  - 对连接不可用（DISABLED / AUTH_REQUIRED）直接响应 403 Forbidden；对事务性拒绝（如版本过期、事实冲突）响应 409 / 400，严禁静默返回 200。
- **测试**：`tests/test_linear_connector.py::test_p1_02_authorization_revision_lifecycle_and_stale_rejection`。

### 7.3 P1-03：Console Webhook 慢速攻击防御与超时保护
- **根因**：Console `ThreadingHTTPServer` 原先在读取请求体时未设置 socket 读取超时，慢速恶意客户端可发送虚高 Content-Length 并缓慢发送字节，挂起服务工作线程。
- **修复**：
  - 在 `console/http_server.py` 对 `/api/intake/webhook/linear` 设置 5.0s 强制读取超时及完整性检查（`len(raw) == n`）。
  - 超时立即返回 408 并关闭 socket，未完整读取直接返回 400 并切断连接。
- **测试**：`tests/test_linear_connector.py::test_p1_03_console_slow_client_read_timeout_and_incomplete_body`。

### 7.4 P2：签名 Body 业务事件类型白名单验证
- **根因**：原先仅在 HTTP Header 层过滤 `Linear-Event`，若缺失 Header 或 Header 伪造，非 Issue 事件（如 Project、Comment）可能被误当作 Issue 处理生成脏任务。
- **修复**：
  - `LinearNormalizer` 严格校验签名 Body 的 `type == "Issue"`，非 Issue 类型抛出 `UnsupportedEventTypeError`。
  - `LinearVerifier` 交叉校验 Header 与 Body 类型一致性，不匹配抛出 400。
  - 有效签名的非 Issue 事件在 Receiver 中安全丢弃并回复 200 IGNORED，不写入 EventStore，不生成 Inbox 任务。
- **测试**：`tests/test_linear_connector.py::test_receiver_http_handler_dispatch`。

---

## 8. 第三轮复审修复记录 (P1: 签名密钥轮换与连接授权版本绑定)

### 8.1 问题背景与攻击面分析
- **漏洞成因**：原先 Webhook 服务（Console 或独立进程）在启动时静态加载单一 `signing_secret`。当管理员在数据库中轮换密钥（`update_connection_authorization(1, auth_ref="new-ref")` $\rightarrow$ `config_revision = 2`）时，未重启的旧服务依然使用内存中旧密钥验签；攻击者用旧密钥伪造新事件，旧服务验证 HMAC 成功并读取数据库中的新版本号 `revision = 2` 盖戳，导致旧密钥突破版本隔离注入事件。
- **治理原则**：
  1. 数据库只存可信 `auth_ref`，绝不存明文密钥；
  2. 签名密钥必须根据数据库当前授权的 `auth_ref` 动态解析；
  3. 服务运行期加载的凭证引用若与数据库当前授权的 `auth_ref` 不一致，**立即 Fail Closed（401 / 403 阻断）**；
  4. 密钥轮换后，旧密钥立即丧失接收资格。

### 8.2 修复实现
1. **可信凭证解析模块**：新建 [herdr/intake/connectors/linear/credentials.py](file:///Users/user/haflow/herdr/intake/connectors/linear/credentials.py)，提供 `resolve_signing_secret()`。实施 Fail-Closed 策略：
   - 检查服务声称持有的 `loaded_auth_ref` 是否与数据库当前快照的 `db_auth_ref` 一致，若不一致抛出 `SecretResolutionError: AUTH_REF_MISMATCH`；
   - 支持环境变量动态多版本解析（`HAFLOW_INTAKE_SECRET_<REF>`、`LINEAR_WEBHOOK_SECRET_<REF>`）与自定义 Resolver。
2. **两套 Webhook 入口统一对齐**：
   - [receiver.py](file:///Users/user/haflow/herdr/intake/connectors/linear/receiver.py#L70-L95)：在单一快照内提取 `(config_revision, auth_ref)`，动态解析对应当前版本的有效密钥并注入 Connector；遇到 `SecretResolutionError` 返回 401。
   - [services/herdr-linear-webhook.py](file:///Users/user/haflow/services/herdr-linear-webhook.py#L35-L65)：新增 `--secret-ref` 参数，将进程凭据引用绑定到 Server。
   - [console/inbox_api.py](file:///Users/user/haflow/console/inbox_api.py#L365-L395)：从 `LINEAR_WEBHOOK_SECRET_REF` 提取凭据引用并注入 handler。

### 8.3 回归测试覆盖
- `tests/test_linear_connector.py::test_p1_secret_rotation_bound_to_authorization_revision`：
  1. Old Secret + revision 1 $\rightarrow$ 200 ACCEPTED；
  2. 轮换密钥至 revision 2 (`auth_ref = ref_v2`)；
  3. 未重启、持有旧 `ref_v1` 的服务收到旧密钥请求 $\rightarrow$ 401 `AUTH_REF_MISMATCH`（Fail Closed）；
  4. 动态解析服务收到旧密钥请求 $\rightarrow$ 401 `INVALID_SIGNATURE`（新密钥验签失败）；
  5. 合法新密钥请求 $\rightarrow$ 200 ACCEPTED（revision 2 正确入库）。

---

## 9. 第五轮复审修复记录 (P2: 贯通双入口动态凭证解析与不可变凭证契约)

### 9.1 问题背景与生产阻断点
1. **Console HTTP 入口前置强校验阻断**：
   - 原 `console/inbox_api.py` 包含 `if not org_id or not secret: return 404...` 校验。
   - 当系统按照不可变凭证架构通过 `HAFLOW_INTAKE_SECRET_<REF>` 或凭证注册表按 `auth_ref` 分发密钥时，全局 `LINEAR_WEBHOOK_SECRET` 为空，导致合法 Webhook 请求被 Console 提前返回 404，未能进入 `resolve_signing_secret()`。
2. **独立 Webhook 守护服务 CLI 强制参数限制**：
   - `services/herdr-linear-webhook.py` 原要求 `if not args.secret: parser.error(...)`，强制要求启动参数提供明文 `--secret` 或全局环境变量。
   - 这阻断了无明文密钥启动、完全按 `auth_ref` 动态加载安全凭据的运维场景。
3. **缺少生产级 HTTP Handler 端到端轮换测试**：
   - 上轮测试直接调用 `handle_linear_webhook`，未完整贯穿 Console `Handler` 与独立 `LinearWebhookHandler` 的 HTTP 请求/响应处理链路（Socket 读写、头部解析、JSON 封装与状态码映射）。

### 9.2 修复实现与契约冻结
1. **统一双入口凭证解析委托**：
   - [console/inbox_api.py](file:///Users/user/haflow/console/inbox_api.py#L380-L396)：放宽前置门禁，仅要求 `LINEAR_ORGANIZATION_ID`；将 `secret`（可为 `None`）和 `secret_ref` 完整委托给 `handle_linear_webhook`，由 `resolve_signing_secret()` 统一解析，未配置时 Fail Closed 返回 401 `CREDENTIAL_UNRESOLVED`。
   - [services/herdr-linear-webhook.py](file:///Users/user/haflow/services/herdr-linear-webhook.py#L35-L50)：将 `--secret` 设为可选参数，允许启动时不传入明文密钥，运行期按数据库当前 `auth_ref` 动态解析。
2. **凭证不可变性契约（Immutable Credential Contract）**：
   - **核心不变量**：同一个 `auth_ref` 对应不可变的密钥版本。
   - **轮换准则**：轮换密钥必须在更新物理密钥的同时更换引用标识（如 `linear-secret-ref-v1` $\rightarrow$ `linear-secret-ref-v2`），并在数据库中调用 `update_connection_authorization` 递增授权版本（`config_revision`）。严禁在保持相同 `auth_ref` 不变的情况下篡改其背后的物理密钥内容。

### 9.3 交付测试证据
1. `tests/test_linear_connector.py::test_console_http_handler_secret_rotation`：
   - 验证无全局 `LINEAR_WEBHOOK_SECRET` 时，Console `Handler` 经 socketpair 接收 HTTP POST，基于环境变量 `HAFLOW_INTAKE_SECRET_LINEAR_REF_V1` 动态解析密钥并成功入库（200 ACCEPTED）；
   - 数据库连接授权轮换至 `ref_v2`（revision 2）后，携带旧密钥签名的请求立即被拒绝（401 INVALID_SIGNATURE）；
   - 携带新密钥签名的请求成功入库（200 ACCEPTED）；
   - 轮换至未配置物理密钥的 `ref_v3` 时，严格 Fail Closed（401 CREDENTIAL_UNRESOLVED）。
2. `tests/test_linear_connector.py::test_standalone_webhook_handler_secret_rotation`：
   - 验证独立 `LinearWebhookHandler` 在 `signing_secret=None` 时完全动态解析；
   - 验证未重启、持有旧 `ref_v1` 的陈旧独立服务接收事件时，触发 `AUTH_REF_MISMATCH` 并 Fail Closed（401）；
   - 验证未配置物理密钥时触发 `CREDENTIAL_UNRESOLVED`（401）。
3. `tests/test_linear_connector.py::test_standalone_cli_parser_allows_omitted_secret`：
   - 验证独立服务命令行在省略 `--secret` 时成功解析参数并启动。
