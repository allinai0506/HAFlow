# 故障自愈与疑难排解 (Troubleshooting & FAQ)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> 本文档汇总 HAFlow 系统在多 Agent 协同、工作流调度、Pane/Tab 状态异常等场景下的常见故障排查与自愈恢复方案。

---

## 1. 现场与工位问题

### 1.1 阶段页签被误关闭怎么办？
- **现象**：在终端多工位现场中不小心关掉了某个阶段的页签。页签不能没有窗格，但系统不再单独留一个名为 `Anchor` 的空壳。
- **排查**：执行 `herdr-task node-status --workflow-id <id>` 查看工位状态。
- **恢复**：下次派发任务或执行以下命令时，系统会把缺失的页签建回来，第一个任务直接占用它自带的窗格：
  ```bash
  herdr-task ensure-runtime --workflow-id <workflow_id> --node <node_id>
  ```

### 1.2 Task 执行完成但 Pane 仍然卡在屏幕上？
- **机制说明**：为了方便排查和复盘，任务执行完成后默认会保留现场 Pane；只有在进入 `cleaned` 阶段或调用清理命令时才进行归档。
- **恢复/清理**：
  ```bash
  # 清理指定任务现场
  herdr-task cleanup <task_id>
  ```

---

## 2. 调度与推进问题

### 2.1 任务执行成功了，为什么后续节点没有自动推进？
- **排查 1（验收准则）**：检查前置任务是否真正达到完成状态（`status=cleaned` 或 `status=completed`）。未达标的任务不会触发下游推进。
- **排查 2（汇聚节点）**：如果下游节点有多个依赖（如标书流程中的 `strategy` 同时依赖 `scoring_extract` 和 `history_search`），必须等待**所有**依赖分支全部完成才会激活下游。
- **排查 3（Controller 服务日志）**：查看控制器日志：
  ```bash
  tail -n 50 ~/.herdr-controller/logs/controller.out.log
  ```

### 2.2 启动 Workflow 提示 `No READY Agent found`？
- **原因**：当前机器上所有的 Agent 均未能通过 Deep Preflight 探针测试（例如 API Token 失效或本地未安装对应 CLI）。
- **排查**：运行 `herdr-factory doctor` 或运行：
  ```bash
  herdr-deep-preflight --deep
  ```
  根据具体的报错提示配置相应 Agent 的环境变量或执行登录。

---

## 3. 并发锁与死锁问题

### 3.1 提示 `Reservation timeout` 或 Agent 无法获取？
- **排查**：检查 `~/.herdr-controller/agent-reservations.json` 是否存在过期死锁记录。
- **恢复**：系统在每次路由时会自动根据 300 秒 (5分钟) TTL 剔除失效预占（代码默认 `_clean_reservations(..., ttl=300)`）；或手动重置预占文件：
  ```bash
  echo '{"reservations": {}}' > ~/.herdr-controller/agent-reservations.json
  ```

---

<a id="orphan-hub-probe"></a>
## 4. 无主 experience-hub 探针

> 适用：某端口被归属不明的 Experience Hub 实例占用——launchd 托管实例（`8780`）、父进程已退出的 ad-hoc 探针，或"进程存活但 db 文件已消失"的存量孤儿（issue #328 现场形态）。本小节是该场景的唯一权威处理手册（runbook）；服务的 launchd 登记见 [`service-management.md` §1](service-management.md)。
> 下文 `herdr-experience-hub` 指本仓库 `bin/herdr-experience-hub`（线上实例跑在 launchd 钉住的 `releases/<sha>` 快照上）；路径一律用 `$HOME`/`~` 可移植写法，不写死本机绝对路径。

### 4.1 先 `status` 归属，再决定动作路径
- **原则**：端口 LISTEN 实测是实例存在性的权威，登记（registry）只做归属增强；归属不明一律 fail-closed，**不得**因"读不到登记"就报"实例不存在"或直接杀进程。
- **第一步（只读）**：`status` 不建库、不建目录、不注册 token、不写登记、不发信号：
  ```bash
  herdr-experience-hub status --port <port> --json
  ```
- **按 `manager` 三态分流**：

  | `manager` | 含义 | 动作 |
  | :--- | :--- | :--- |
  | `launchd` | 由 `com.user.herdr-experience-hub` 托管（KeepAlive=true、ThrottleInterval=5s） | `stop` 拒绝发信号并返回 exit 6；走 `launchctl bootout`/`kickstart`，见 4.4。直接 `kill` 它会被 5 秒复活。 |
  | `ad_hoc` | 终端/脚本直接拉起的探针实例 | 可停：进程通过三重 AND 校验（持有选中 `(host,port)` 的 LISTEN ∧ cmdline 属 hub 特征 ∧ 登记与选择器一致）后发 SIGTERM，见 4.2。 |
  | `unknown` | `launchctl` 查询失败/超时/输出无法解析 | `stop` 拒绝发信号并返回 exit 5；先修复探测环境（4.6），再重新 `status`；期间不做任何终止动作。 |

- **退出码速查**（`status`/`stop` 共用）：`0` 成功或幂等收敛（`stop` 对 absent 同样返回 0）/ `2` 用法错误（缺选择器、`--port 0`、非法 `--host`/`--pid` 组合）/ `3` 不存在（absent / stale，仅 `status` 报告用）/ `4` ambiguous、foreign、degraded，或 `--db-path` 无匹配 / `5` 无法探测（`lsof`/`ps` 缺失或解析失败；`manager=unknown` 时的 `stop`）/ `6` launchd 托管拒绝 / `7` SIGTERM 已发但 5s 内未退出 / `1` 仅未预期异常。**禁止**把 foreign/ambiguous/launchd/unverifiable 吞成 `0`。

### 4.2 停掉 ad-hoc 探针（`stop`）
- 先用 `status`（或 4.6 只读组合）逐个 pid 确认归属与 `(host, port)`，再执行：
  ```bash
  herdr-experience-hub stop --port <port>
  ```
- 同端口多监听者时（macOS 上 `127.0.0.1:P` 与 `0.0.0.0:P` 可由两个进程同时 LISTEN），目标必须唯一：用 `--host <host>` 或 `--pid <pid>` 缩小范围，否则返回 exit 4，工具不替你猜。
- `stop` 成功 = 返回前复探选中 `(host,port)` 已无任何 LISTEN；只发 SIGTERM、最多等 5 秒；超时返回 exit 7、保留登记、不升级信号。
- **禁止按进程名批量终止**：`pkill -f herdr-experience-hub` 会连同 `8780` 生产实例一起杀（cmdline 完全匹配），随后被 launchd 复活，制造一次无谓的生产抖动。
- **禁止 `kill -9`**：强杀绕过端口复探判定，结果不可归因。v1 的 hub 进程不安装 SIGTERM 处理器，`stop` 的成功判定完全依赖外部复探，不依赖被停进程自清理。
- 仅当该代际快照尚未部署 `stop` 子命令时，才允许对已确认归属的单个 pid 人工 `kill <pid>`（默认 SIGTERM），且逐个确认后再动手。
- SQLite 关闭竞态提示：SIGTERM 不走 SQLite 优雅关闭，`stop` 后立即用同一 DB 启动若失败（短暂锁竞态），**先重试一次**再排查其他原因。

### 4.3 登记与 db 文件边界
- 登记位置：`$HOME/.herdr-controller/run/experience-hub/<pid>.json`；只有 `start` 在 bind 成功后写登记，`status` 永不写。
- `stop` 清理登记时**只删与选择器匹配的条目，不删 db 文件**，也不删他人条目；exit 7（超时）保留登记。
- 登记根目录不存在 = 空登记，不是错误；解析失败的条目按 `corrupt` 处理，`stop` 不读取它、也不凭它杀进程。

### 4.4 `launchctl` 的 bootout 与 kickstart 区别
- `launchctl kickstart -k`：**重启**——杀掉当前进程并按已加载的 job 配置立即拉起新进程，**不重读 plist**；用它"停止"会得到杀了又复活的假象。
- `launchctl bootout`：**停用并卸载**——job 从 launchd 移除，KeepAlive 不再复活；要再拉起须重新 `bootstrap`。
  ```bash
  launchctl print gui/$(id -u)/com.user.herdr-experience-hub        # 只读：确认 job 状态与当前 pid
  launchctl bootout gui/$(id -u)/com.user.herdr-experience-hub      # 停用并卸载
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.herdr-experience-hub.plist
  launchctl kickstart -k gui/$(id -u)/com.user.herdr-experience-hub # 原地重启，不重读 plist
  ```
- `8780` 被孤儿占用时，launchd 托管实例的 fail-fast 会以 ThrottleInterval=5s 节奏刷日志——这是期望的 fail-fast，不是新 bug；按本小节处置。

### 4.5 `db_exists=false` + `state=running` 的读法
- 该组合表示"进程仍在服务一个已消失的事实源"（issue #328 现场形态：存量孤儿进程、其 db 所在目录已被清理）。须人工判断，工具不自动干预。
- 脚本**不得**把 `db_exists=false` 单独当"服务不可用"告警，也**不得**只看 `state=running` 就当"一切正常"——本通道不判定 hub 的业务健康度。
- `db_path` 归属顺序：登记条目 → argv 中的 `--db-path` → `null`；为 `null` 时 `db_exists` 也是 `null`，不回退默认路径冒充事实。

### 4.6 只读排查命令与输出安全
- v1 不提供 `status --all` 全端口扫描；人工排查端口占用用下面的只读组合（`lsof` 的 `-p` 与 `-i` 必须加 `-a` 才是 AND 关系，陷阱详见 [`service-management.md`](service-management.md) §2.2.2 折叠块）：
  ```bash
  lsof -nP -iTCP:<port> -sTCP:LISTEN
  ps -o pid=,command= -p "$(lsof -nP -iTCP:<port> -sTCP:LISTEN -t | head -1)" \
    | sed -E 's/(--admin-token|--token)([= ])[^ ]+/\1\2<redacted>/g'
  ```
- 任何 cmdline 回显（文档示例、工具报错、工单与日志粘贴）必须把 `--admin-token`、`--admin-token=…`、`--token` 的值统一替换为 `<redacted>`；`--json` 输出的 `listeners[]` 只含 pid/host/port，不含 argv。argv 可见是既存暴露面（另 issue 评估），新输出不得放大泄漏。
- `lsof`/`ps` 缺失或输出不可解析（Linux 常见）时，`status`/`stop` 报 unverifiable（exit 5）并停手，不得猜测归属；macOS 为第一支持平台。

### 4.7 存量孤儿接管顺序（清理动作需现场授权）
终止存量孤儿属宿主副作用操作，不在代码自动清理范围；本手册只给接管路径，且第 2 步起任何清理动作执行前须获得现场授权：
1. 先 `status` 确认归属与 `manager`（4.1），多实例逐个核对；
2. `manager=ad_hoc` 且目标唯一 → `herdr-experience-hub stop --port <port>`；`launchd` → 按 4.4 走 `launchctl`；`unknown` → 不动作，先修复探测；
3. 仅当该代际尚未部署新工具时，对逐个确认过的 pid 人工 `kill <pid>`；禁止 `kill -9`、禁止 `pkill -f`；
4. 禁止脚本越界批量清理未经确认的进程。

## 5. Workflow 启动未完成与启动失败

### 5.1 新建 Workflow 被“已有活跃 Workflow”拒绝，且标题/阶段为空、无任务
- **现象**：同项目新建 Workflow 返回 exit 2，提示“项目 ... 已有活跃 Workflow，拒绝重复创建”；被指向的 Workflow 在 Console 中标题、阶段为空，且没有任何任务。
- **原因**：Factory 先注册（`status=running`、`startup_ready=false`）后跑 Deep Preflight。注册之后、就绪之前的异常/中断若未被收敛（例如进程被强杀），会留下永久未就绪记录；active guard 只看 `status != completed`，因此持续拦截。
- **自动处置**：正常失败由 Factory 在抛出原错误前收敛为 `completed + outcome=startup_failed`；进程被强杀的遗留由 Controller 看门狗在超过期限后回收（默认 1800s，每 60s 最多扫描一次、单轮最多回收 20 条）。项目在记录进入终态后即可重新创建。
- **人工清理**（看门狗不动作或需要立即解除阻塞时）：
  ```bash
  herdr-task close-workflow <workflow_id> --abandon
  ```
- **识别记录**：见 [ops-center §8](../wiki/ops-center.md) 的 SQL；只把 JSON 显式 `false` 视为未就绪，缺失/null/数字 0/字符串 "false" 与损坏 JSON 都不会被回收。
- **阈值**：`HERDR_WORKFLOW_STARTUP_DEADLINE_SECONDS` 默认 1800；非数字/≤0/≤210/溢出回退默认并只告警一次（`[STARTUP WATCHDOG CONFIG]`）。
- **看门狗不回收的边界**：记录任意状态已有任务；`startup_ready=true`；`paused/in_progress/closing/failed`；foreign（`workflow_file` 缺失/已删除或位于 `/tmp/`、`pytest-` 路径）；Controller 停机期间。以上场景用人工 `close-workflow --abandon` 兜底。
- **日志**：`[STARTUP WATCHDOG RECLAIMED]` 表示已自动回收；`[STARTUP WATCHDOG CONFIG]` 表示期限配置被回退；`[WORKFLOW STARTUP CONVERGE FAILED]` 表示 Factory 自身收敛失败（记录保持 `running`，交由看门狗）；`[STARTUP WAIT]` 每 Workflow 首次 1 行、之后每 600s 最多 1 行，不再无限刷屏。
- **验证过的消费者行为**：`completed + outcome=startup_failed` 不触发交付校验、不产生完成通知、不触发自动收尾；Controller 不再推进该 Workflow；看板完成数统计口径未变。
