# 后台守护进程与服务运维指南 (Service Management)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> 本文档说明 HAFlow 系统的 macOS LaunchAgent 后台守护进程配置、服务生命周期管理与日志排查手段。

---

## 1. 服务清单

系统由 4 个独立的后台服务共同协作组成：

| 服务标识 (Label) | 脚本路径 | 核心职责 |
| :--- | :--- | :--- |
| `com.user.herdr-controller` | `/Users/user/HAFlow/services/herdr-controller.py` | 任务状态监控、DAG 依赖推进、协调器事件分发。 |
| `com.user.herdr-notifier` | `/Users/user/HAFlow/services/herdr-notifier.py` | 任务与工作流完成时的 macOS 原生系统通知推送。 |
| `com.user.herdr-sentinel` | `/Users/user/HAFlow/services/herdr-sentinel.py` | 工位生命周期监控与空闲/僵死任务守护巡检。 |
| `com.user.herdr-factory-console` | 仓库源：`console/herdr_factory_console.py`；部署副本：`/Users/user/.herdr-console/herdr_factory_console.py` | 可视化 Web 控制台服务（默认端口 `8765`）。 |

Console 使用 Python 标准库 `http.server` 提供 HTTP 服务，页面是内嵌 HTML/CSS/原生 JavaScript，没有独立 Node/npm 构建产物。Dashboard API 由 `/api/ops-center` 暴露；Workflow 启动通过异步 `POST /api/run` + `GET /api/run/status` 完成，具体契约见 [`console/README.md`](../../console/README.md) 与 [`wiki/ops-center.md`](../../wiki/ops-center.md)。

---

## 2. 常用运维管理命令

### 2.1 查看服务运行状态
```bash
launchctl list | grep herdr
```
正常输出示例（第一列为系统 PID，第二列为最近退出码 `0`）：
```text
64501   0   com.user.herdr-controller
90611   0   com.user.herdr-notifier
90613   0   com.user.herdr-sentinel
68120   0   com.user.herdr-factory-console
```

### 2.2 重启服务（热更新代码后生效）

> ⚠️ **先看清部署拓扑。** Console 与 Controller 的 launchd 配置写死
> `HERDR_ROOT=~/.herdr-controller/releases/<40位commit>`，跑的是**按 commit 冻结的
> `git archive` 快照**；Sentinel 与 Notifier 则直跑工作区 `~/HAFlow/`。因此：
>
> - 改了 **sentinel / notifier**（`~/HAFlow/services/`）→ `kickstart` 即生效；
> - 改了 **console / controller** 或 `herdr/` 包 → **工作区改动不会生效**，
>   必须走 §2.2.1 的完整流程。
> - `./scripts/install-herdr-console.sh` 只把 console 脚本复制到 `~/.herdr-console/`，
>   **不改 plist**，在当前拓扑下那份副本不会被执行。

```bash
# 仅适用于 sentinel / notifier，或"配置未变"的纯进程重启
launchctl kickstart -k gui/$(id -u)/com.user.herdr-controller
launchctl kickstart -k gui/$(id -u)/com.user.herdr-factory-console
launchctl kickstart -k gui/$(id -u)/com.user.herdr-notifier
```

#### 2.2.1 让 console / controller 真正加载新代码

`launchctl kickstart` **只重启进程，不重读 plist**（launchd 用已加载的 job 配置快照）。
改了 plist 后必须 `bootout` + `bootstrap`。

```bash
# 1. 提交改动，确保快照有对应 commit
git rev-parse HEAD

# 2. 按该 commit 重建 release 快照（保留旧快照作回滚）
SHA=$(git rev-parse HEAD)
mkdir -p ~/.herdr-controller/releases/$SHA
git archive --format=tar $SHA | tar -x -C ~/.herdr-controller/releases/$SHA

# 3. 改 plist 指向新快照（console 要改两处：HERDR_ROOT + ProgramArguments）
R=/Users/user/.herdr-controller/releases/$SHA
P=~/Library/LaunchAgents/com.user.herdr-factory-console.plist
PlistBuddy -c "Set :EnvironmentVariables:HERDR_ROOT $R" $P
PlistBuddy -c "Set :ProgramArguments:1 $R/console/herdr_factory_console.py" $P
PlistBuddy -c "Set :ProgramArguments:1 $R/services/herdr-controller.py" \
  ~/Library/LaunchAgents/com.user.herdr-controller.plist

# 4. 重载 job（顺序：先改 plist，再 bootout，最后 bootstrap）
for j in com.user.herdr-factory-console com.user.herdr-controller; do
  launchctl bootout gui/$(id -u)/$j
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$j.plist
done

# 5. 部署后三查：PID -> 实际加载路径 -> 与 HEAD 比对
ps -o command= -p "$(lsof -nP -iTCP:8765 -sTCP:LISTEN -t | head -1)" | grep -oE '[a-f0-9]{40}'
git rev-parse HEAD    # 两者必须一致
```

<details>
<summary>排查端口占用时的 lsof 陷阱</summary>

`lsof -p PID -iTCP` 中 `-p` 与 `-i` 是 **OR** 关系，会把该进程所有 fd
（含 dylib、`/dev/null`）**加上系统上全部网络连接**一并列出。必须用 `-a`：

```bash
lsof -a -nP -p <PID> -iTCP -sTCP:LISTEN   # 只看该进程的 LISTEN 端口
```

不加 `-a` 时，一个 console 进程曾被误报为"监听 98 个端口"（实含 3306/6379/3000
等他人服务），真实 LISTEN 只有 1 个。

</details>

### 2.3 停止与重新加载服务
```bash
# 停止 Controller
launchctl bootout gui/$(id -u)/com.user.herdr-controller

# 加载 Controller Plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.herdr-controller.plist
```

---

## 3. 日志文件与排查路径

所有服务输出的日志均统一存放在用户主目录下的 `.herdr-controller/logs/`：

| 日志文件 | 监控排查内容 |
| :--- | :--- |
| `~/.herdr-controller/logs/controller.out.log` | 任务轮询、状态转移、推进触发标准输出。 |
| `~/.herdr-controller/logs/controller.err.log` | 异常报错、崩溃追踪、网络通信异常。 |
| `~/.herdr-controller/logs/notifier.out.log` | 通知发送记录。 |
| `~/.herdr-controller/logs/console.out.log` | Web Console 请求日志。 |

### 实时日志跟踪命令：
```bash
tail -f ~/.herdr-controller/logs/controller.out.log
```
