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

## 可执行证据与运行版本边界

`FACT` binary_present、request_verified、interactive_ready 是三种独立事实。exit0、空响应或仅输入回显不构成请求成功；认证、模型和配额错误保留具体分类。不可查询的配额标 unknown。预检身份包括实际二进制/配置内容指纹及启动模式，漂移或 TTL 到期需重新探测；二进制读取有大小、时间及读取前后身份边界。

`FACT` Worker 在业务提示派发前核对真实工位身份及交互就绪，信任对话不会被自动批准。启动失败且副作用无法证明时保留资源与恢复记录，不凭 Pane ID 回收未知工位。原生 API 不提供可靠的 stop/kill 或 Tab 实例身份时，不假设具备此能力。

`FACT` 安装器构造完整 ProgramArguments 与对应 HERDR_ROOT，release 内容、执行权限必须与新鲜 git archive 一致；安装器验证后清理字节码缓存，release 服务禁写缓存，运行证明遇到未验证缓存时标 unknown。先验证整个 plist 批次，再逐文件原子发布并备份；未知用户参数要求明确迁移。运行启动指纹和配置指纹分开：doctor 没有当前运行证明时返回 unknown，不把配置更新、目录名称或 HTTP200 当作实际加载版本。自动测试只使用临时 HOME/DB 和 --no-restart，不调用 launchctl。

Evidence:
- `herdr/deep_preflight.py#preflight_identity`
- `herdr/agent_adapter.py#startup_readiness`
- `services/herdr-worker.py#main`
- `herdr/service_release.py#publish_service_plists`
- `herdr/service_release.py#runtime_fingerprint`
- `scripts/install-herdr-console.sh`
- `tests/test_preflight_runtime_contract.py`
- `tests/test_worker_readiness_contract.py`
- `tests/test_service_release.py`

相关页面：[[task-lifecycle]]。

## Workflow定向及部分刷新

`FACT` `herdr-deep-preflight --workflow-id <wf> --agent <agent> --deep --apply`使用真实已注册adapter请求探针，并回写StateStore及兼容投影。部分刷新只更新preflight_agent_checked_at与该Agent identity/健康结论；仅配置完整全池结果齐备时更新preflight_checked_at，不能把局部探针年龄写成全局新鲜。

`FACT` Router对过期软故障候选先执行现有deep请求验证，只有READY、request_verified及verifiable identity才选择；首选失败可继续其它兼容候选，显式指定Agent不会被静默替换。单Agent新鲜证据可单独采用，binary/config/root/mode指纹漂移则重新核验。硬故障不会因TTL过期自动解除，只能显式定向刷新重验。历史无身份字段的新鲜快照仍有兼容路径，不声称已迁移所有旧记录。

`UNKNOWN` 请求探针可能涉及真实模型费用；本轮只验证受控transport，没有生产探针或运行服务更新。clone内worker预检失败仍走launch recovery，不在该阶段跨身份重复创建资源。

Evidence:
- `herdr/deep_preflight.py#refresh_workflow_preflight`
- `herdr/agent_router.py#choose_agent`
- `tests/test_fix_bug1002_routing.py`
- `tests/test_preflight_runtime_contract.py`
