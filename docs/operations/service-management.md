# 后台守护进程与服务运维指南 (Service Management)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> 本文档说明 HAFlow 系统的 macOS LaunchAgent 后台守护进程配置、服务生命周期管理与日志排查手段。

---

## 1. 服务清单

后台服务分两代运行方式，**纳管清单的唯一权威是 `herdr/service_release.py` 里的 `SCRIPTS`**：

| 服务标识 (Label) | 脚本路径 | 核心职责 |
| :--- | :--- | :--- |
| `com.user.herdr-controller` | `/Users/user/HAFlow/services/herdr-controller.py` | 任务状态监控、DAG 依赖推进、协调器事件分发。 |
| `com.user.herdr-notifier` | `/Users/user/HAFlow/services/herdr-notifier.py` | 任务与工作流完成时的 macOS 原生系统通知推送。 |
| `com.user.herdr-sentinel` | `/Users/user/HAFlow/services/herdr-sentinel.py` | 工位生命周期监控与空闲/僵死任务守护巡检。 |
| `com.user.herdr-factory-console` | 仓库源：`console/herdr_factory_console.py`；部署副本：`/Users/user/.herdr-console/herdr_factory_console.py` | 可视化 Web 控制台服务（默认端口 `8765`）。 |
| `com.user.herdr-experience-hub` | `bin/herdr-experience-hub`（`start --host 127.0.0.1 --port 8780 --db-path ~/.herdr-controller/experience_hub.db`） | 企业经验中心：内网审核发布业务规则，独立 SQLite + `http.server`，契约见 [`wiki/enterprise-experience-hub.md`](../../wiki/enterprise-experience-hub.md)。 |

**Experience Hub 探针生命周期**：`stop` 对 launchd 托管实例拒绝发信号（exit 6）；托管实例的停用/重启用 `launchctl bootout` / `bootstrap` / `kickstart -k`（命令形态见 §2.3：`bootout` 停用并卸载，`kickstart -k` 仅原地重启且**不重读 plist**）。ad-hoc 探针与存量孤儿先 `status` 按 `manager` 三态分流再处置，禁止 `pkill -f` 与 `kill -9`。唯一权威手册：[故障排查 FAQ · 无主 experience-hub 探针](troubleshooting-faq.md#orphan-hub-probe)。

新增服务只在 `SCRIPTS` 登记一次：部署脚本用 `python3 -m herdr.service_release --labels` 取清单，不再自备第二份。带启动参数的服务在登记时声明**参数词表**（如 `('start', '--host', '--port', '--db-path')`）——词表校验 flag，参数值属宿主状态，逐字保留。

未登记而 plist 又钉在 `releases/<sha>` 上的服务会被部署脚本拒绝部署（见 §2.2.1），因为它会永久停在旧代际且不报错。

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
>   必须先提交，再重建快照并让服务重载。

#### 2.2.1 推荐：一条命令部署（`SCRIPTS` 里的全部服务）

`./scripts/install-herdr-console.sh` 会：按 `HEAD` 重建 release 快照 →
改 plist（`ProgramArguments`、`HERDR_ROOT`、`WorkingDirectory` 一起迁移，不留分裂代际）→
`bootout` + `bootstrap` 重载 → 自检运行中进程是否真的加载了新 commit。改完直接跑它即可。

```bash
./scripts/install-herdr-console.sh              # 部署当前 HEAD
./scripts/install-herdr-console.sh --sha <commit>  # 部署指定 commit
./scripts/install-herdr-console.sh --no-restart   # 写 plist 配置，但不重载（运行进程仍是旧代码）
./scripts/install-herdr-console.sh --allow-unmanaged <label>  # 显式豁免未纳管服务，让它继续钉在旧 release 上
```

部署前先做**未纳管检查**：脚本枚举 `~/Library/LaunchAgents/com.user.herdr-*.plist`，
凡是钉在 `releases/<sha>` 却不在 `SCRIPTS` 里的服务，会连同「落后 HEAD 多少个提交」一起报错并**拒绝部署**
（不写任何 plist）。这类漂移过去不报错，只会在某天该服务代码真的变了才被发现。

**改 console / controller 前必须先提交**，否则快照还是旧 commit。

#### 2.2.2 手动流程（脚本不可用时；或你要看每一步细节）

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
plutil -replace EnvironmentVariables.HERDR_ROOT -string "$R" "$P"
plutil -replace ProgramArguments.1 -string "$R/console/herdr_factory_console.py" "$P"
plutil -replace ProgramArguments.1 -string "$R/services/herdr-controller.py" \
  ~/Library/LaunchAgents/com.user.herdr-controller.plist
plutil -lint "$P"

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
