# Deep Preflight 探针手册 (Deep Preflight Playbook)

> 本文档说明 Herdr Deep Preflight 机制的原理、各 Agent 探针适配逻辑与健康诊断方法。

---

## 1. 为什么需要 Deep Preflight？

在过去实验中，单纯通过 `which <agent>` 检查二进制是否存在是远远不够的：
- Agent 依赖的远程 API Key 可能已过期；
- Agent CLI 可能陷入交互式登录授权死循环；
- 某些 Agent 在后台 LaunchAgent 环境下因找不到 Login Shell 环境变量而报错。

**Deep Preflight 机制**通过沙盒执行极小微任务（如让 Agent 返回 `READY` 并立即退出），确保分发任务前该 Agent 确实处于健康可响应状态。

---

## 2. 支持的 Agent 与探针适配机制

系统在 `herdr/deep_preflight.py`（CLI: `herdr-deep-preflight`）中为各大主流 Agent 提供了专门的无副作用探针：

| Agent | 探针适配策略 | 超时时间 | 验收准则 |
| :--- | :--- | :--- | :--- |
| `claude` | 无头单回合模式 (`claude --print "output READY..."`) | 90s，TIMEOUT 自动重试 1 次 | 进程返回 0 且包含 `READY` |
| `codex` | 静默命令模式 (`codex exec "echo READY..."`) | 40s | 进程返回 0 且包含 `READY` |
| `opencode` | 极简执行 (`opencode run "echo READY..."`) | 40s | 进程返回 0 且包含 `READY` |
| `qodercli` | 脚本模式 (`qodercli --print "print READY..."`) | 40s | 进程返回 0 且包含 `READY` |
| `agy` | CLI 简短交互 (`agy --print "output READY..."`) | 40s | 进程返回 0 且包含 `READY` |
| `pi` | 非交互模式 (`pi --print --no-session "READY..."`) | 40s | 进程返回 0 且包含 `READY` |
| `grok` | 单回合模式 (`grok -p "READY..."`) | 40s | 进程返回 0 且包含 `READY` |
| `kimi` | 单回合模式 (`kimi -p "READY..."`) | 40s | 进程返回 0 且包含 `READY` |

> 校准依据（2026-09-15 实测）：`claude --print` 冷启动一次 36.9s 成功、另一次 60s 仍无输出，
> 统一 35s 阈值会把健康但慢的执行者稳定误判为 TIMEOUT，故 claude 单独 90s + 超时重试。
> `TIMEOUT` 仅表示单次采样超时，不触发 `--auto-disable`；`PROVIDER_ERROR`（服务端过载/5xx/连接失败/模型不存在）
> 与 `TOKEN_EXHAUSTED` / `AUTH_REQUIRED` 一样计入建议禁用候选；`LOCAL_ERROR`（CLI 本地基础设施故障，如文件 watcher
> 启动失败、ENOENT/EACCES）计入建议禁用候选但不触发 `--auto-disable`（多为偶发）。快速失败（≤15s）的
> `PROVIDER_ERROR` 自动重试 1 次以区分抖动与持续中断。控制台「执行者自检」弹窗同时展示每路探针
> 原始输出尾部（800 字符），以便人工复核分类是否准确。

---

## 3. 手动执行与排障

### 3.1 运行全量沙盒检测
```bash
herdr-deep-preflight --deep
# 或完整路径：
python3 /Users/user/HAFlow/bin/herdr-deep-preflight --deep
```

### 3.2 针对特定项目排查
```bash
herdr-deep-preflight --project-id xiyu-bid-poc --deep
```

### 3.3 输出 JSON 诊断报告
```bash
herdr-deep-preflight --deep --json | jq .
```
