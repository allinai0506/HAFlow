# Claude 开发与执行指引 (CLAUDE.md)

> **上海共事智能科技有限公司 · 共事 · HAFlow**  
> *让人和多个 AI Agent 一起把事情做完 (Human + Agent, in Flow)*  
> 本文件为 Claude / AI Assistant 在本仓库工作时的**核心执行入口**。  
> 涵盖关键路径、日常开发调试命令、变更验收清单与高频环境避坑指南。

---

## 1. 关键系统路径 (Key Paths & Layout)

```text
/Users/user/HAFlow/
├── bin/                          # CLI 执行入口 (herdr-factory, herdr-task, preflight)
├── services/                     # LaunchAgent 后台服务脚本 (controller, sentinel, notifier, worker)
├── herdr/                        # 核心业务包 (workflow, agent_router, pane_pool, projects, topology)
├── workflow_templates/           # 声明式 DAG 模板 (YAML)
├── tests/                        # 单元与集成测试 (test_workflow_engine.py)
└── docs/                         # 分级结构化业务与技术文档

运行时状态目录 (Runtime State):
~/.herdr-controller/
├── tasks.json                    # 全局 Task 工单注册表
├── projects.json                 # 多项目绑定与配置中心
├── workflows.json                # 运行中/已归档 Workflow 元数据
├── stage-policies.json           # 阶段策略配置
├── workflows/<wf>/shared/        # Workflow 共享文档区 (append-only notes.jsonl, clone 外)
├── logs/                         # 后台服务日志 (controller.out.log, etc.)
└── clones/                       # Task 隔离运行的独立 Git CoW 克隆工作区
```

---

## 2. 常用开发与运维命令 (Commands)

### 2.1 自动化测试
```bash
# 运行全量测试套件
pytest

# 运行详细测试并显示用例输出
pytest -v tests/test_workflow_engine.py
```

### 2.2 系统体检与探活
```bash
# 执行 Factory 全局环境体检 (核心文件、服务状态、项目绑定)
./bin/herdr-factory doctor

# 列出所有可用工作流模板
./bin/herdr-factory templates

# 执行快速 Agent 准入体检
./bin/herdr-preflight

# 执行沙盒化深层探针 (实测模型与 Token 连通性)
./bin/herdr-deep-preflight --deep
```

### 2.3 后台服务管理 (macOS LaunchAgent)
```bash
# 查看所有后台常驻服务运行状态
launchctl list | grep herdr

# 热重启 Controller 调度进程 (代码修改后必须执行)
launchctl kickstart -k gui/$(id -u)/com.user.herdr-controller

# 热重启 Sentinel 僵死看门狗
launchctl kickstart -k gui/$(id -u)/com.user.herdr-sentinel

# 热重启 Notifier 通知服务
launchctl kickstart -k gui/$(id -u)/com.user.herdr-notifier

# 跟踪 Controller 实时日志
tail -f ~/.herdr-controller/logs/controller.out.log
```

### 2.4 代码检查与静态验证
```bash
# 全库 Python 语法与字节码编译检查
python3 -m compileall herdr/ services/ bin/ tests/
```

---

## 3. 严格验收清单 (Verification Checklist)

提交或宣布任务完成前，必须逐项完成以下核对：

- [ ] **统一研发流程合规 (Unified Dev Flow)**：全流程遵循 `/unified-dev-flow` S0–S8 闭环与九大核心不变量；S0 远端同步与 CoW 沙盒建支无违规；S5 具备新鲜实测验证铁证；S8 完成知识与 Wiki 沉淀。
- [ ] **目录结构合规**：严禁向根目录随意添加文件；新代码必须归入 `herdr/`、`services/` 或 `bin/`。
- [ ] **分层与核心纯度**：业务决策逻辑必须下沉为纯函数（`herdr/`）；CLI 与守护进程仅做编排装配与 I/O，严禁内嵌复杂决策。
- [ ] **文件健康度合规**：单文件规模符合梯度拆分阈值（`herdr/` 核心 300~500 行，CLI/服务 500~800 行），无机械切碎亦无超长混乱单文件。
- [ ] **自动化测试 100% 通过**：运行 `pytest`，确保所有 16 个测试全部通过且无警告。
- [ ] **全局体检 PASS**：运行 `./bin/herdr-factory doctor`，确保无 `FAIL` 项。
- [ ] **守护进程健康**：运行 `launchctl list | grep herdr`，确认各服务 PID 正常且退出码为 `0`。
- [ ] **无残留临时垃圾**：检查无未加入 `.gitignore` 的 `.tmp`、`.bak`、调试日志或临时测试脚本。
- [ ] **文档与代码同步**：若修改了 CLI 选项、模块结构、核心算法、状态机或服务路径，必须同步更新 `wiki/`、`README.md`、`AGENTS.md` 及 `docs/` 相应文档。

---

## 4. 环境坑点与排障防雷 (Environment Gotchas)

### ⚠️ 坑点 1：LaunchAgent 进程更新陷阱
- **现象**：修改了 `services/herdr-controller.py` 或 `herdr/` 模块，但后台调度的行为依然是旧逻辑。
- **原因**：LaunchAgent 进程常驻在内存中，不会自动热加载 Python 源码。
- **正解**：代码修改后，必须运行 `launchctl kickstart -k gui/$(id -u)/com.user.herdr-controller` 进行优雅热重载。严禁用终端直接 `python3 services/herdr-controller.py` 重复启动（会发生 Unix Socket / 状态文件冲突）。

### ⚠️ 坑点 2：CoW Clone 变化识别与验收假象
- **现象**：在任务 Clone 目录中运行普通 `git status`，显示大量未提交文件，误判为 Agent 生成的代码。
- **原因**：CoW Clone 创建时会保留 Task 派发时主干工作区的未提交改动。
- **正解**：严禁使用裸 `git status`！必须使用 `./bin/herdr-task verify-baseline <task-id>`，以系统打下的 Git Tree 快照为准，只有列在 `TASK_CHANGED` 下的文件才是当前 Task 的真实产出。

### ⚠️ 坑点 3：Tab 与 Anchor Pane 误关与易失性
- **现象**：在多工位终端现场中误关了某个阶段的 Tab 或 Anchor 终端，担心任务崩溃或找不到工位。
- **原因**：Tab ID 和 Pane ID 只是动态的运行时缓存，真正的逻辑身份是 Node ID / Label。
- **正解**：HAFlow 在任务派发前具备自动检测并自愈能力。只要触发任务派发或调用 `ensure_node_runtime`，系统会自动发现并重新创建对应 Tab 和 Anchor Pane，无需手动重建。

### ⚠️ 坑点 4：`No READY Agent found` 路由阻断
- **现象**：启动工作流或派发任务时，提示找不到可用 Agent。
- **原因**：Workflow 启动时执行了 Deep Preflight 沙盒体检，该工作流只允许分发给检测通过的 `healthy_agents`。若机器上没有可用 Agent（如 Claude/Codex 凭证过期），路由会主动阻断以防死锁。
- **正解**：运行 `./bin/herdr-deep-preflight --deep` 查看各个 Agent 的真实报错，根据提示登录 CLI 或配置环境变量。

### ⚠️ 坑点 5：Python 模块导入路径解析
- **现象**：从 `services/` 或 `bin/` 启动脚本时提示 `ModuleNotFoundError: No module named 'herdr'`。
- **正解**：所有可执行脚本必须在顶部动态解析 `HERDR_ROOT` 并加入 `sys.path`，业务导入统一使用 `from herdr.xxx import yyy`。
