# 体检与沙盒健康探针 (preflight-and-health.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> **轻量探活、Deep Preflight 沙盒深探与 Token/Auth 模式识别**  
> 关联索引: [[index]] | [[system-overview]] | [[agent-routing-and-pools]] | [[architecture]]

---

## 1. 双层体检架构定位

为了避免向无法连通、凭证过期或配额耗尽的 Agent 盲目派发任务造成流水线死锁，HAFlow 建立了分层的准入体检机制：

```mermaid
graph LR
    subgraph L1 [1. 轻量静态体检 (herdr-preflight)]
        BinCheck[CLI 二进制检测] --> VerCheck[--version 版本探测]
        VerCheck --> AuthHintCheck[本地配置文件存在性]
    end

    subgraph L2 [2. 沙盒动态深探 (herdr-deep-preflight)]
        SandboxInit[创建临时隔离沙盒] --> NonDestructProbe[执行极简无副作用指令]
        NonDestructProbe --> PatternMatch[正则表达式分类器]
        PatternMatch --> Classify[判定: READY / TOKEN_LIMIT / AUTH_EXPIRED]
    end

    L1 --> L2
    L2 --> WriteHealthy[回写 healthy_agents 供 Router 消费]
```

Evidence:
- `herdr/preflight.py`
- `herdr/deep_preflight.py`
- `docs/operations/deep-preflight-playbook.md`

---

## 2. 轻量静态体检 (`herdr-preflight`)

`FACT` 用于日常快速巡检，耗时毫秒级，不消耗任何模型 Token：
1. **二进制可执行探测**: 检查 `opencode`, `codex`, `claude`, `qodercn`, `agy`, `pi`, `grok`, `kimi` 是否可解析。解析顺序与机制：`herdr/agent_binary.py` 自动将用户主目录工具链（`USER_BIN_DIRS`，如 `~/.opencode/bin`, `~/.local/bin`, `~/.volta/bin` 等）前置注入当前进程 `PATH`，确保用户空间最新安装永远优先于系统/Homebrew 残留旧版本；若 PATH 未命中则遍历 `EXTRA_BIN_DIRS`，最后以登录 shell `command -v` 终极兜底。LaunchAgent 服务的精简 PATH 不再导致已装 CLI 被误判为"未安装"或命中系统陈旧版本。
2. **版本号探测**: 带 8 秒超时的 `--version` 探活，防止二进制由于系统动态链接库缺失而僵死。
3. **本地凭证提示 (`AUTH_HINTS`)**:
   - Codex: `~/.codex/auth.json`
   - Claude: `~/.claude.json`
   - Pi: `~/.pi/agent/auth.json`
   - Grok: `~/.grok/auth.json`
   - OpenCode: `~/.config/opencode`
   - QoderCLI: `~/.qoder-cn`
   - Kimi: `~/.kimi-code/credentials/kimi-code.json`

Evidence:
- `herdr/agent_binary.py:AGENT_BINARIES`（agent id -> CLI 映射单一事实来源）
- `herdr/agent_binary.py:resolve_binary` / `resolve_agent_binary`
- `herdr/preflight.py:AUTH_HINTS`
- `herdr/preflight.py#probe_version`

---

## 3. 深层沙盒动态体检 (`herdr-deep-preflight`)

### 3.1 探针无副作用安全契约 (Non-Destructive Safety)
> [!IMPORTANT]
> **探针安全红线**  
> `deep-preflight` 探活时，严禁让 Agent 生成大批量代码或触发高昂 Token 消耗。  
> 探针必须在临时沙盒中通过极简空指令（如 `"echo READY"` 或最小 ping 请求）验证连通性。

### 3.2 异常分类与正则模式库
`FACT` 探针通过对 CLI 输出进行保守的正则特征匹配，精确区分故障原因：

| 故障类别 | 正则匹配模式 (Patterns) | 判定结果 |
| :--- | :--- | :--- |
| **Token / 配额耗尽** | `token.*(exhaust\|limit\|quota)`<br/>`quota.*exhaust`<br/>`rate.?limit`<br/>`premium.*limit` / `out of credits` / `billing.*limit` / `402` | `TOKEN_EXHAUSTED` |
| **认证失效 / 未登录** | `not logged in`<br/>`authentication required`<br/>`unauthorized`<br/>`invalid api key`<br/>`expired.*key` / `access denied` / `401` / `403` | `AUTH_REQUIRED` |
| **服务端过载 / 不可用** | `overloaded`<br/>`server error` / `service unavailable` / `50x`<br/>`model.*not found`<br/>`connection refused/reset` | `PROVIDER_ERROR` |
| **CLI 本地基础设施故障** | `watcher did not become ready`<br/>`unexpected critical error`<br/>`ENOENT` / `EACCES` / `EPERM` | `LOCAL_ERROR` |
| **工作区未授权信任** | `do you trust`<br/>`workspace trust` | `TRUST_REQUIRED` |
| **正常就绪** | 正常返回且未匹配任何阻断规则 | `READY` |

`FACT` 超时与重试口径（按执行者分别校准，单次采样超时不等同不可用）：
1. **分执行者超时**: `claude` 90s，其余 40s。依据为实测 `claude --print` 冷启动 36.9s 成功、偶发 60s 仍无输出，统一短阈值会把健康但慢的执行者稳定误判为 `TIMEOUT`。
2. **超时重试一次**: 仅 `claude` 超时后自动重试 1 次；重试成功记 `READY`，仍超时才判 `TIMEOUT` 并注明“可能是慢而非不可用”。快速失败（≤15s）的 `PROVIDER_ERROR` 同样重试 1 次以区分抖动与持续中断；慢失败与通用 `ERROR` 保持单样本。
3. **`TIMEOUT` 与 `LOCAL_ERROR` 不触发 `--auto-disable`**；`PROVIDER_ERROR` 与 `TOKEN_EXHAUSTED` / `AUTH_REQUIRED` 一样计入建议禁用候选。控制台「执行者自检」弹窗展示每路探针原始输出尾部供人工复核。
4. **`pi` 已接入真实探针**（`pi --print --no-session`，无副作用）：`UNKNOWN` 仅保留给尚无确认安全非交互模式的执行者。

### 3.3 Claude 工作区信任自动铺路
针对 Claude Code 常见的 `"do you trust this folder"` 阻塞对话框，系统在任务启动前（`services/herdr-worker.py#ensure_claude_workspace_trust`）自动向 `~/.claude.json` 注入 `hasTrustDialogAccepted = True`，从根源消除交互式卡死。

Evidence:
- `herdr/deep_preflight.py:TOKEN_PATTERNS`
- `herdr/deep_preflight.py:AUTH_PATTERNS`
- `herdr/deep_preflight.py:PROVIDER_PATTERNS`
- `herdr/deep_preflight.py:LOCAL_PATTERNS`
- `herdr/deep_preflight.py:SMOKE_TIMEOUTS` / `smoke_probe`
- `herdr/deep_preflight.py:choose_smoke_command` (pi `--print --no-session`)
- `services/herdr-worker.py#ensure_claude_workspace_trust`
- `RULES.md:探针无副作用安全`
