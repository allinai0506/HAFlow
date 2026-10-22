# HAFlow 操作人特权凭据鉴权与严格模式部署指南 (Issue #307)

> 上海共事智能科技有限公司 · 共事 · HAFlow
> 门禁豁免与特权操作接入企业级凭据鉴权与主体能力绑定

---

## 1. 架构与安全原则

HAFlow 针对门禁强制放行 (`force_pass_gate`)、任务门禁豁免 (`bypass_gate`) 和特权复核 (`force_review`) 等高危控制面操作，建立了全链路企业级凭据鉴权与主体能力绑定机制。

### 核心安全不变量
1. **默认零凭据 Fail-Closed**：出厂默认状态下，任何未携带有效凭据的特权操作一律拒绝并阻断，禁止任何无凭据放行（Fail-Open）。
2. **Worker 工位不可伪造与自豁免阻断**：处于 Worker 工位、任务克隆目录 (`/.herdr-controller/clones/`) 或具有 Worker 环境变量的主体，被物理与逻辑双重隔离，严禁签发凭据或执行门禁豁免。
3. **高熵密钥与原子 0600 落盘**：系统 HMAC 签名密钥采用密码学随机源 (`secrets.token_hex(32)`) 生成，通过 `O_CREAT | O_EXCL` 和临时文件原子重命名生成，文件权限严格限制为 `0600`，彻底消除硬编码公共常量与低熵预测攻击。
4. **会话与操作人凭据严格分离**：Web 控制台经验中心会话凭据 (`experience_hub`) 与特权操作人凭据彻底解耦，特权操作必须使用专用的 `X-HAFlow-Operator-Token` 请求头或 `operator_token` 载荷字段。
5. **主体与能力显式绑定**：操作人凭据绑定唯一的角色 (`role`) 和最小必要能力集合 (`capabilities`)，遵循 `gate_bypass_policy.yaml` 约束。

---

## 2. 生产部署与环境变量

### 环境变量参考表

| 变量名 | 默认值 | 作用说明 | 部署建议 |
| --- | --- | --- | --- |
| `HERDR_OPERATOR_AUTH_STRICT` | `1` (生产默认开启) | 强制启用严格模式。未携带有效签名 Token 或注册凭据的请求直接拒绝。 | 生产环境保持默认 `1`。 |
| `HERDR_OPERATOR_SIGNING_SECRET` | 自动从凭据目录读取 | 控制器签名 HMAC Token 所用的主密钥（至少 32 字符十六进制高熵串）。 | 生产环境推荐由 KMS 或 Secret Manager 统一注入。 |
| `HERDR_HMAC_SECRET` | 空 | 备用 HMAC 签名密钥配置项。 | 可选兼容项。 |
| `HERDR_OPERATOR_TOKEN` | 空 | 允许操作人或管理脚本通过进程环境传递已签发的高权限 Token。 | CLI 或自动化运维脚本可按需设置。 |
| `HERDR_ALLOW_UNAUTHENTICATED_OPERATOR` | `0` (生产默认禁用) | 仅供历史无凭据兼容测试使用，生产环境与 Worker 环境强制禁用。 | 生产环境严禁配置为 `1`。 |

---

## 3. 密钥生成与轮换流程

### 3.1 首次初始化
控制器守护进程在首次启动时，若未显式配置 `HERDR_OPERATOR_SIGNING_SECRET`，会自动检查受保护的凭据目录（默认为 `~/.herdr-controller/credentials/`）：
1. 凭据目录不存在时自动以 `0700` 权限创建。
2. 通过 `secrets.token_hex(32)` 生成 256 位高熵随机密钥。
3. 通过独占模式 (`os.O_CREAT | os.O_EXCL`) 创建临时文件，原子权限设为 `0600`。
4. 写入完成后原子重命名为 `~/.herdr-controller/credentials/operator_signing.key`。

### 3.2 密钥轮换机制 (Key Rotation)
当主签名密钥发生泄露风险或遵循合规周期轮换时，按以下步骤执行无缝轮换：
1. **生成新密钥**：
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```
2. **更新控制器配置**：
   将新密钥写入持久化密钥文件或更新环境变量 `HERDR_OPERATOR_SIGNING_SECRET`：
   ```bash
   install -m 0600 /dev/null ~/.herdr-controller/credentials/operator_signing.key
   echo -n "<NEW_SECRET_HEX>" > ~/.herdr-controller/credentials/operator_signing.key
   ```
3. **重新签发操作人 Token**：
   旧 Token 在密钥轮换后立即失效，管理员使用新密钥为授权操作人重新发放 Token。

---

## 4. 凭据发放与 CLI 管理入口

凭据发放仅允许在控制器宿主环境执行，Worker 环境严禁调用。

### 4.1 CLI 命令发放 Token

HAFlow 提供了原生的 CLI 命令进行操作人 Token 签发：

```bash
# 语法
bin/herdr-task issue-operator-token <OPERATOR_ID> [选项]
# 或
bin/herdr-factory issue-operator-token <OPERATOR_ID> [选项]
```

#### 选项说明
- `--role`：指定操作人角色，可选值：`human_operator`（默认）、`admin`、`system_admin`、`security_auditor`。
- `--capabilities`：逗号分隔的能力列表，例如 `bypass_gate,force_pass_gate,force_review`。若省略则自动应用角色的默认能力。
- `--expires-in`：Token 有效期秒数（默认 3600 秒即 1 小时，传 0 表示永久）。
- `--secret`：显式指定签名密钥（默认使用环境变量或已初始化的受控密钥）。
- `--out-credentials`：将生成的 JSON 凭据保存到指定文件（自动设置 0600 权限）。

#### 示例
```bash
# 为首席运维工程师签发 2 小时有效期的 admin 令牌
bin/herdr-task issue-operator-token ops_lead \
  --role admin \
  --expires-in 7200 \
  --out-credentials ~/.herdr-controller/credentials/ops_lead.json

# 输出示例：
# [OK] Operator token successfully issued for 'ops_lead'
# Token: haflow_v1.eyJjYXBzIjpbImJ5cGFzc19nYXRlIiwiZm9yY2VfcGFzc19nYXRlIiwiZm9yY2VfcmV2aWV3Il0sImV4cCI6MTcyOTU3NDAwMCwiaWF0IjoxNzI5NTY2ODAwLCJyb2xlIjoiYWRtaW4iLCJzdWIiOiJvcHNfbGVhZCJ9.7fa8e1...
# Credentials file written to: /Users/user/.herdr-controller/credentials/ops_lead.json (mode 0600)
```

---

## 5. 特权操作的使用范式

### 5.1 CLI 调用门禁豁免
```bash
# 携带 Token 执行 bypass-gate
bin/herdr-task bypass-gate task-build-01 \
  --operator ops_lead \
  --token "<TOKEN_STRING>" \
  --reason "PR 已经过人工离线验收并附带测试回执"

# 或携带凭据文件路径
bin/herdr-task bypass-gate task-build-01 \
  --operator ops_lead \
  --credentials ~/.herdr-controller/credentials/ops_lead.json \
  --reason "PR 已经过人工离线验收并附带测试回执"
```

### 5.2 Web 控制台与 HTTP API 调用
特权操作 API 采用独立的请求头传递操作人 Token：
- **HTTP Header**：`X-HAFlow-Operator-Token: <TOKEN_STRING>`
- **请求体 JSON 字段**：`"operator_token": "<TOKEN_STRING>"`

例如调用 `/api/controller/execute-action` 执行放行：
```http
POST /api/controller/execute-action HTTP/1.1
Host: 127.0.0.1:57000
Content-Type: application/json
X-HAFlow-Console-Request: 1
X-HAFlow-Operator-Token: haflow_v1.ey...

{
  "workflow_id": "wf-deploy-01",
  "type": "force_pass_advance",
  "task_id": "task-test-01",
  "gate_node_id": "test",
  "expected_version": 2,
  "confirmed": true,
  "reason": "紧急热修复旁路验证通过",
  "operator": "ops_lead"
}
```

---

## 6. 审计与追踪验证

所有经过凭据鉴权的操作，其回执与状态记录中均严格保留凭据强度：
- `authenticated: True`
- `auth_method: "hmac_token"` 或 `"state_db_token"`
- `operator: <OPERATOR_ID>`
- `role: <ROLE>`

任何未携带凭据或伪造凭据的操作，绝不会以 `authenticated: False` 产生弱审计通过，直接抛出 `OperatorAuthenticationError` 并记录安全事件告警。
